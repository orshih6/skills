#!/usr/bin/env python3
"""selftest: check dispatch.py without an account and without starting a session.

Standard library only. Everything happens in one temporary directory that is removed
at the end: each check gets its own DISPATCH_HOME, its own git repository and its own
stand-in `claude` executable, which is first on PATH. The stand-in records how it was
called, prints `backgrounded · abc12345` for a start, and answers
`claude agents --json --all` and `claude rm` from files the check writes.

    python3 skills/dispatch/scripts/selftest.py        every check
    python3 skills/dispatch/scripts/selftest.py wait   only checks whose name holds "wait"

Prints one line per check (`ok` or `FAIL` with the reason) and a one-line total.
Exits 0 when every check passes and 1 otherwise. Starting a cloud session is not
covered (it needs a pseudo-terminal and GitHub); reading one is, with a stand-in API.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True  # importing dispatch.py must not leave a __pycache__ beside it

DISPATCH = Path(__file__).resolve().parent / "dispatch.py"

BRIEF = """\
# Add a price column

Show the price of each product in the table on the products page.
Done means: `make test` passes."""

FAKE_CLAUDE = r'''
import json, os, shutil, sys

d = os.environ["FAKE_CLAUDE_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\n")


def state(name, default):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return default
    with open(p) as f:
        return f.read()


if args == ["agents", "--json", "--all"]:
    sys.stdout.write(state("agents.json", "[]"))
elif args[:1] == ["rm"]:
    target = state("rm_removes", "").strip()  # the worktree a real `claude rm` would remove
    if target:
        shutil.rmtree(target, ignore_errors=True)
    sys.stdout.write(state("rm_output", "removed\n"))
elif "--bg" in args:
    out = state("start_output", None)
    if out is None:
        out = "backgrounded · " + state("next_id", "abc12345").strip() + "\n"
    sys.stdout.write(out)
else:
    sys.stderr.write("stand-in claude: unexpected arguments %r\n" % (args,))
    sys.exit(2)
'''


class Failure(Exception):
    pass


def expect(cond, reason):
    if not cond:
        raise Failure(reason)


def same(got, want, what):
    expect(got == want, f"{what}: got {got!r}, want {want!r}")


def has(text, part, what):
    expect(part in text, f"{what}: {part!r} is missing from {text.strip()[-300:]!r}")


def refused(r, part, what, code=1):
    """The command stopped with this exit code and this text on stderr."""
    same(r.returncode, code, f"{what}: exit code")
    has(r.stderr, part, f"{what}: stderr")


class Env:
    """One check's world: a DISPATCH_HOME, a git repository and a stand-in claude."""

    def __init__(self, root, name):
        self.dir = root / name
        self.home = self.dir / "dispatch"
        self.fake = self.dir / "fake"
        self.repo = self.dir / "shop"
        bindir = self.dir / "bin"
        for d in (self.home, self.fake, self.repo, bindir):
            d.mkdir(parents=True)
        (self.fake / "claude.py").write_text(FAKE_CLAUDE)
        exe = bindir / "claude"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.fake / "claude.py"}" "$@"\n')
        exe.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            DISPATCH_HOME=str(self.home), FAKE_CLAUDE_DIR=str(self.fake), HOME=str(self.dir),
            PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Self Test", GIT_AUTHOR_EMAIL="selftest@example.invalid",
            GIT_COMMITTER_NAME="Self Test", GIT_COMMITTER_EMAIL="selftest@example.invalid",
        )
        self.git("init", "-q", "-b", "main")
        self.commit("README.md")

    # git ---------------------------------------------------------------------------

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise Failure(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout.strip()

    def commit(self, filename, cwd=None):
        (Path(cwd or self.repo) / filename).write_text(f"{filename}\n")
        self.git("add", filename, cwd=cwd)
        self.git("commit", "-q", "-m", f"add {filename}", cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def commit_on(self, branch, filename):
        """What a session in its worktree would leave: a commit on its own branch."""
        self.git("checkout", "-q", "-B", branch)
        tip = self.commit(filename)
        self.git("checkout", "-q", "main")
        return tip

    # dispatch.py -------------------------------------------------------------------

    def d(self, *args):
        return subprocess.run([sys.executable, str(DISPATCH), *map(str, args)], env=self.env,
                              cwd=self.dir, capture_output=True, text=True, timeout=60)

    def local(self, name, *flags, text=BRIEF, repo=None, new_id=None):
        if new_id:
            (self.fake / "next_id").write_text(new_id)
        f = self.dir / f"brief-{len(list(self.dir.glob('brief-*')))}.md"
        f.write_text(text + "\n")
        return self.d("local", "--repo", repo or self.repo, "--name", name, "--brief-file", f, *flags)

    def started(self, name, *flags, **kw):
        r = self.local(name, *flags, **kw)
        expect(r.returncode == 0, f"local {name} failed: {r.stderr.strip()}")
        return r

    def reviewed(self, name, new_id="abc12346"):
        """Give the task a commit on its branch and start its review."""
        self.commit_on(f"worktree-{name}", f"{name}.txt")
        (self.fake / "next_id").write_text(new_id)
        r = self.d("review", name)
        expect(r.returncode == 0, f"review {name} failed: {r.stderr.strip()}")
        return r

    # state -------------------------------------------------------------------------

    def tasks(self):
        f = self.home / "tasks.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def task(self, name):
        found = [t for t in self.tasks() if t["name"] == name]
        expect(len(found) == 1, f"tasks.jsonl holds {len(found)} lines named {name}, want 1")
        return found[0]

    def calls(self):
        f = self.fake / "calls.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def starts(self):
        return [c for c in self.calls() if "--bg" in c["args"]]

    def agents(self, **states):
        """What `claude agents --json --all` answers: session id -> state."""
        rows = [{"id": i, "state": s, "name": "x"} for i, s in states.items()]
        (self.fake / "agents.json").write_text(json.dumps(rows))

    def config(self, **cfg):
        (self.home / "config.json").write_text(json.dumps(cfg))

    def report(self, name, text):
        (self.home / "results").mkdir(exist_ok=True)
        (self.home / "results" / f"{name}.md").write_text(text)

    def brief_of(self, name):
        return (self.home / "briefs" / f"{name}.md").read_text()

    def status(self, cmd=("status",)):
        """Run status (or wait) and return {task name: (where, state, repo, note)}."""
        r = self.d(*cmd)
        expect(r.returncode == 0, f"{' '.join(cmd)} failed: {r.stderr.strip()}")
        rows = {}
        for line in r.stdout.splitlines():
            m = re.match(r"(\S+)\s+(\S+)\s+(\S+)\s+(\d+)m  (\S+)  (.*)$", line)
            if m:
                rows[m.group(1)] = (m.group(2), m.group(3), m.group(5), m.group(6))
        return rows, r.stdout


DONE = """\
STATUS: done
BRANCH: worktree-alpha
PR: none
TESTS: make test, passed
SUMMARY: Added the price column.
"""

REVIEW_FINDINGS = """\
STATUS: done
VERDICT: findings
RISK: high
CHECKS: make test
    ok 12 tests
FINDINGS:
1. MAJOR  CONFIRMED  shop/table.py:14  the price is empty for a product with no variants
2. MINOR  PLAUSIBLE  shop/table.py:30  the column is not sorted for a price of 0
"""

REVIEW_CLEAN = """\
STATUS: done
VERDICT: clean
RISK: low
CHECKS: make test, passed
FINDINGS: none
"""

CHECKS = []


def check(name):
    def wrap(fn):
        CHECKS.append((name, fn))
        return fn
    return wrap


# local ---------------------------------------------------------------------------------


@check("local: the task line")
def local_task_line(e):
    head = e.git("rev-parse", "HEAD")
    before = int(time.time())
    r = e.started("alpha")
    same(r.stdout.splitlines()[0],
         "started local alpha id=abc12345 repo=shop branch=worktree-alpha", "first line of output")
    has(r.stdout, "note: this repo has a branch with no upstream", "note about work a worktree may not see")
    same(e.calls()[0]["args"], ["agents", "--json", "--all"], "first claude call (the limit check)")
    same(len(e.tasks()), 1, "lines in tasks.jsonl")
    t = e.task("alpha")
    started = t.pop("started")
    expect(isinstance(started, int) and before <= started <= before + 60, f"started is {started!r}, not now")
    same(t, {"name": "alpha", "where": "local", "id": "abc12345", "repo": str(e.repo),
             "branch": "worktree-alpha", "base": head, "worktree": True, "needs_local": False},
         "task line")

    e.started("beta", new_id="abc12346")
    same([t["name"] for t in e.tasks()], ["alpha", "beta"], "a second task is appended")
    same(e.task("beta")["id"], "abc12346", "id of the second task")


@check("local: the brief file with the rules appended")
def local_brief_file(e):
    e.report("alpha", "STATUS: done\nSUMMARY: a report left by an earlier task of the same name\n")
    e.started("alpha")
    text = e.brief_of("alpha")
    result = e.home / "results" / "alpha.md"
    expect(text.startswith(BRIEF + "\n\n## Rules for this dispatched session\n"),
           "the brief file does not start with the brief followed by the rules")
    same(text.count("## Rules for this dispatched session"), 1, "times the rules appear")
    for part in ("Put your work on the branch `worktree-alpha`.",
                 "Commit your work to `worktree-alpha` and leave it on this machine. Do not push",
                 "Do not ask questions and wait",
                 "## Reporting (required, last step)",
                 f"Write your result to `{result}` in exactly this shape",
                 "STATUS: done | failed | blocked",
                 "`STATUS: handoff`"):
        has(text, part, "brief file")
    left = re.findall(r"\{(?:branch|push|extra|result|notify)\}", text)
    expect(not left, f"placeholders were not filled in: {left}")
    expect("notify the owner" not in text, "a notify command is in the brief with none configured")
    expect(not result.exists(), "the report of an earlier task with the same name was not removed")

    e.config(extra_rules="- Never touch the billing tables.", notify="notify.sh '{name} finished'")
    e.started("beta", new_id="abc12346")
    text = e.brief_of("beta")
    has(text, "say what happened.\n- Never touch the billing tables.", "extra_rules after the rules")
    has(text, "notify the owner once with this command", "notify text")
    has(text, "    notify.sh 'beta finished'\n", "notify command with the task name filled in")


@check("local: the worktree flags")
def local_worktree_flags(e):
    e.started("alpha")
    c = e.starts()[0]
    prompt = e.brief_of("alpha")[:-1]
    same(c["args"][:-1], ["--bg", "-n", "alpha", "-w", "alpha"], "flags of the start")
    same(c["args"][-1], prompt, "prompt given to claude (the brief file without its last newline)")
    same(c["cwd"], str(e.repo), "directory claude was started in")
    exclude = e.repo / ".git" / "info" / "exclude"

    r = e.started("beta", "--no-worktree", "--needs-local", new_id="abc12346")
    same(e.starts()[1]["args"][:-1], ["--bg", "-n", "beta"], "flags with --no-worktree")
    t = e.task("beta")
    same((t["branch"], t["worktree"], t["needs_local"]), ("dispatch/beta", False, True),
         "branch, worktree, needs_local with --no-worktree --needs-local")
    has(e.brief_of("beta"), "Put your work on the branch `dispatch/beta`.", "brief with --no-worktree")
    expect("note:" not in r.stdout, "the worktree note is printed for a task that has no worktree")
    same(exclude.read_text().splitlines().count(".claude/worktrees/"), 1,
         "times .claude/worktrees/ is in .git/info/exclude after the first worktree start")
    e.started("delta", new_id="abc12348")
    same(exclude.read_text().splitlines().count(".claude/worktrees/"), 1,
         "times .claude/worktrees/ is in .git/info/exclude after the second worktree start")

    plain = e.dir / "notes"
    plain.mkdir()
    r = e.started("gamma", repo=plain, new_id="abc12347")
    c = e.starts()[-1]
    same(c["args"][:-1], ["--bg", "-n", "gamma"], "flags in a folder that is not a git repository")
    same(c["cwd"], str(plain), "directory claude was started in for a plain folder")
    t = e.task("gamma")
    same((t["branch"], t["base"], t["worktree"]), (None, None, False),
         "branch, base, worktree in a folder that is not a git repository")
    has(r.stdout, "repo=notes branch=None", "output for a plain folder")


@check("local: a session that does not start")
def local_start_failures(e):
    (e.fake / "start_output").write_text("Error: something went wrong\n")
    r = e.local("alpha")
    refused(r, "could not start the session", "no `backgrounded` line")
    has(r.stderr, "something went wrong", "what claude printed is shown")
    same(e.tasks(), [], "tasks after a failed start")

    (e.fake / "start_output").write_text("This folder is not trusted yet.\n")
    refused(e.local("alpha"), "is not a trusted folder", "untrusted folder", code=3)
    same(e.tasks(), [], "tasks after an untrusted folder")

    (e.fake / "start_output").write_text("\x1b[1mbackgrounded\x1b[0m · \x1b[36mdeadbeef\x1b[0m  alpha\n")
    e.started("alpha")
    same(e.task("alpha")["id"], "deadbeef", "id read from coloured output")


# refusals of local ---------------------------------------------------------------------


def nothing_started(e, want_tasks, what):
    same(len(e.starts()), len(want_tasks), f"{what}: sessions started")
    same([t["name"] for t in e.tasks()], want_tasks, f"{what}: tasks")


@check("local refuses: a bad name")
def refuse_bad_name(e):
    for name in ("Bad_Name", "a", "has space", "x" * 42, "trailing/slash", "UPPER"):
        refused(e.local(name), "name must be lowercase letters, digits and dashes", f"name {name!r}")
    nothing_started(e, [], "after bad names")
    expect(not (e.home / "briefs").exists(), "a brief file was written for a refused name")
    for i, name in enumerate(("a1", "x" * 41, "2-fix")):
        e.started(name, new_id=f"abc1234{i}")


@check("local refuses: a duplicate name")
def refuse_duplicate(e):
    e.started("alpha")
    brief = e.brief_of("alpha")
    r = e.local("alpha", text="# Something else entirely", new_id="abc12346")
    refused(r, "a task named alpha already exists", "second task named alpha")
    nothing_started(e, ["alpha"], "after a duplicate")
    same(e.brief_of("alpha"), brief, "brief of the first task after the refusal")


@check("local refuses: an empty brief")
def refuse_empty_brief(e):
    for text in ("", "   \n\n\t"):
        refused(e.local("alpha", text=text), "the brief is empty", f"brief {text!r}")
    nothing_started(e, [], "after empty briefs")


@check("local refuses: the parallel limit")
def refuse_limit(e):
    ids = [f"aaaaaa0{i}" for i in range(6)]
    for i in range(4):
        e.agents(**{x: "working" for x in ids[:i]})
        e.started(f"task-{i}", new_id=ids[i])
    names = [f"task-{i}" for i in range(4)]
    e.agents(**{x: "working" for x in ids[:4]})
    r = e.local("task-4", new_id=ids[4])
    refused(r, "4 local tasks are already running (limit 4)", "fifth task with the default limit")
    nothing_started(e, names, "at the default limit")

    e.report("task-0", DONE)  # a report does not free a place: the limit counts live sessions
    e.agents(**{ids[0]: "done", ids[1]: "blocked", ids[2]: "working", ids[3]: "working"})
    e.config(max_parallel=2)
    refused(e.local("task-4", new_id=ids[4]), "2 local tasks are already running (limit 2)",
            "limit from config.json")
    nothing_started(e, names, "at the configured limit")

    e.agents(**{ids[0]: "done", ids[1]: "blocked", ids[2]: "working"})
    e.started("task-4", new_id=ids[4])
    e.agents()
    e.started("task-5", new_id=ids[5])


# review --------------------------------------------------------------------------------


@check("review refuses: no commits on the branch")
def review_no_commits(e):
    e.started("alpha")
    refused(e.d("review", "alpha"), "the branch worktree-alpha does not exist", "branch not created")
    e.git("branch", "worktree-alpha")
    refused(e.d("review", "alpha"), "worktree-alpha has no commits of its own", "branch at its base")
    e.commit("later.txt")  # the default branch moving on does not give the task commits
    refused(e.d("review", "alpha"), "worktree-alpha has no commits of its own",
            "branch at its base, default branch ahead")
    refused(e.d("review", "nobody"), "no task named nobody", "unknown task")

    plain = e.dir / "notes"
    plain.mkdir()
    e.started("gamma", repo=plain, new_id="abc12347")
    refused(e.d("review", "gamma"), "gamma has no branch", "task in a folder that is not a git repository")
    nothing_started(e, ["alpha", "gamma"], "after refused reviews")
    expect(not (e.home / "briefs" / "alpha-review.md").exists(), "a review brief was written for a refusal")


@check("review: starts with the right diff range")
def review_starts(e):
    base = e.git("rev-parse", "HEAD")
    e.started("alpha")
    e.commit("later.txt")  # main moves on: the range must still start where the task started
    e.report("alpha-review", "STATUS: done\nVERDICT: clean\n")
    r = e.reviewed("alpha")
    same(r.stdout.strip(),
         f"started review alpha-review id=abc12346 repo=shop diff={base[:12]}..worktree-alpha", "output")
    c = e.starts()[-1]
    prompt = c["args"][-1]
    same(c["args"][:-1], ["--bg", "-n", "alpha-review", "-w", "alpha-review"], "flags of the start")
    same(c["cwd"], str(e.repo), "directory claude was started in")
    same(e.brief_of("alpha-review"), prompt + "\n", "brief file of the review")
    result = e.home / "results" / "alpha-review.md"
    for part in (f"## The task it was given\n\n{BRIEF}\n\n## What to review",
                 f"The change is `worktree-alpha` compared with `{base[:12]}`",
                 f"    git diff --stat {base[:12]} worktree-alpha\n",
                 f"    git diff {base[:12]} worktree-alpha\n",
                 "`git checkout --detach worktree-alpha`",
                 f"Write your result to `{result}` in exactly this shape",
                 "VERDICT: clean | findings"):
        has(prompt, part, "review brief")
    for part in ("## Rules for this dispatched session", "Put your work on the branch",
                 f"results/alpha.md"):
        expect(part not in prompt, f"the review brief holds {part!r}, which belongs to the task's own rules")
    left = re.findall(r"\{(?:brief|base|ref|result|extra|notify)\}", prompt)
    expect(not left, f"placeholders were not filled in: {left}")
    expect(not result.exists(), "the report of an earlier review was not removed")
    t = e.task("alpha-review")
    t.pop("started")
    same(t, {"name": "alpha-review", "where": "local", "kind": "review", "of": "alpha",
             "id": "abc12346", "repo": str(e.repo), "branch": None, "worktree": True,
             "needs_local": True}, "task line of the review")
    same(e.task("alpha")["id"], "abc12345", "the reviewed task is still listed")

    e.config(extra_rules="- Never touch the billing tables.", notify="notify.sh '{name} finished'")
    e.started("beta", new_id="abc12347")
    e.reviewed("beta", new_id="abc12348")
    prompt = e.starts()[-1]["args"][-1]
    has(prompt, "- Never touch the billing tables.\n\n## Reporting", "extra_rules in the review brief")
    has(prompt, "    notify.sh 'beta-review finished'\n", "notify command with the review's name")


@check("review refuses: a second review and a review of a review")
def review_refusals(e):
    e.started("alpha")
    e.reviewed("alpha")
    refused(e.d("review", "alpha"), "alpha already has a review", "second review")
    refused(e.d("review", "alpha-review"), "alpha-review is itself a review", "review of a review")
    nothing_started(e, ["alpha", "alpha-review"], "after refused reviews")

    r = e.d("forget", "alpha-review")
    expect(r.returncode == 0, f"forget alpha-review failed: {r.stderr.strip()}")
    e.agents(abc12345="working")
    e.config(max_parallel=1)
    refused(e.d("review", "alpha"), "1 local tasks are already running (limit 1)", "review at the limit")
    e.agents()
    (e.home / "briefs" / "alpha.md").unlink()
    refused(e.d("review", "alpha"), "the brief of alpha is missing", "review with no brief")
    same([t["name"] for t in e.tasks()], ["alpha"], "tasks after more refused reviews")
    same(len(e.starts()), 2, "sessions started in all (the task and its first review)")


@check("fix: sends MAJOR CONFIRMED findings back to a new round, up to max_fix_rounds")
def fix_checks(e):
    e.started("alpha")
    e.reviewed("alpha")  # a commit on worktree-alpha, and its review abc12346
    wt = e.repo / ".claude" / "worktrees" / "alpha"
    e.git("worktree", "add", "-q", str(wt), "worktree-alpha")
    e.agents(abc12345="done", abc12346="done")

    refused(e.d("fix", "alpha"), "has no finished review", "fix before the review reported")
    e.report("alpha-review", REVIEW_CLEAN)
    refused(e.d("fix", "alpha"), "nothing MAJOR and CONFIRMED", "fix after a clean review", code=7)
    e.report("alpha-review", REVIEW_FINDINGS)
    refused(e.d("fix", "alpha-review"), "is a review", "fix of a review")

    (e.fake / "next_id").write_text("abc12347")
    r = e.d("fix", "alpha")
    same(r.returncode, 0, f"fix exit code ({r.stderr.strip()})")
    has(r.stdout, "started fix alpha-fix1 id=abc12347", "fix output")
    has(r.stdout, "round 2 of 3, 1 finding", "fix output: round and findings")
    start = e.starts()[-1]
    same(Path(start["cwd"]).resolve(), wt.resolve(), "the round runs in the task's own worktree")
    expect("-w" not in start["args"], "the round was given a new worktree")
    prompt = start["args"][-1]
    has(prompt, "Show the price of each product", "the round's brief holds the task")
    has(prompt, "MAJOR  CONFIRMED  shop/table.py:14", "the round's brief holds the confirmed finding")
    expect("PLAUSIBLE" not in prompt.split("## Rules")[0], "an unconfirmed finding was sent back")
    has(prompt, "branch `worktree-alpha`", "the round works on the task's branch")
    t = e.task("alpha-fix1")
    same((t["kind"], t["of"], t["round"], t["branch"], t["base"]),
         ("fix", "alpha", 1, "worktree-alpha", e.task("alpha")["base"]), "the round's task line")
    rows, _ = e.status()
    same(rows["alpha-fix1"][0], "fix", "status shows a round as fix")

    refused(e.d("fix", "alpha"), "has no finished review", "a new round before this one is reviewed")
    e.agents(abc12347="done")
    e.commit_on("tmp", "x.txt")  # (keep main's checkout as it was)
    e.commit("fix1.txt", cwd=wt)
    e.report("alpha-fix1", DONE)
    (e.fake / "next_id").write_text("abc12348")
    r = e.d("review", "alpha-fix1")
    same(r.returncode, 0, f"review of a round ({r.stderr.strip()})")
    has(e.brief_of("alpha-fix1-review"), "A review sent this work back", "its review reads the round's brief")
    e.report("alpha-fix1-review", REVIEW_FINDINGS)
    (e.fake / "next_id").write_text("abc12349")
    r = e.d("fix", "alpha")
    same(r.returncode, 0, f"second round ({r.stderr.strip()})")
    has(r.stdout, "alpha-fix2", "second round's name")
    e.config(max_fix_rounds=2)
    refused(e.d("fix", "alpha"), "already sent back 2 times", "a round beyond max_fix_rounds", code=7)


# status and wait -----------------------------------------------------------------------


@check("status: running, other session states, no tasks")
def status_running(e):
    rows, out = e.status()
    same(out.strip(), "no dispatched tasks", "status with no tasks")
    e.started("alpha")
    for cli, state, note in (("working", "running", ""),
                             ("blocked", "waiting-for-you", "claude attach abc12345"),
                             ("done", "stopped-no-report", "claude logs abc12345"),
                             (None, "gone", "session no longer listed and left no report")):
        e.agents(**({"abc12345": cli} if cli else {}))
        rows, out = e.status()
        expect("alpha" in rows, f"no line for alpha in {out!r}")
        same(rows["alpha"], ("local", state, "shop", note), f"line when the session is {cli or 'not listed'}")
    same(len(out.splitlines()), 1, "lines of status for one task")


@check("status: a written report wins over a session still listed as working")
def status_report_wins(e):
    e.started("alpha")
    e.started("beta", new_id="abc12346")
    e.agents(abc12345="working", abc12346="working")
    e.report("alpha", DONE)
    rows, _ = e.status()
    same(rows["alpha"], ("local", "done", "shop", "Added the price column."), "alpha, reported and still working")
    same(rows["beta"], ("local", "running", "shop", ""), "beta, working with no report")
    e.report("beta", "STATUS: blocked\nBRANCH: none\nPR: none\nTESTS: not run\nSUMMARY:\n"
                     "Needs the price list.\nWhich currency should it show?\n")
    e.agents()
    rows, _ = e.status()
    same(rows["beta"], ("local", "blocked", "shop", "Needs the price list. Which currency should it show?"),
         "beta, reported blocked and session gone")
    e.report("alpha", "STATUS: done\nSUMMARY: " + "long " * 40 + "\n")
    rows, _ = e.status()
    same(len(rows["alpha"][3]), 90, "length of a long note")


@check("status: a review's note")
def status_review_note(e):
    e.started("alpha")
    e.reviewed("alpha")
    e.agents(abc12345="done", abc12346="working")
    rows, _ = e.status()
    same(rows["alpha-review"], ("review", "running", "shop", ""), "review with no report")
    e.report("alpha-review", REVIEW_FINDINGS)
    rows, _ = e.status()
    same(rows["alpha-review"], ("review", "done", "shop", "findings, risk high, 2 findings (1 major confirmed)"),
         "review that reported findings")
    same(rows["alpha"][:2], ("local", "stopped-no-report"), "the reviewed task keeps its own line")
    e.report("alpha-review", REVIEW_CLEAN)
    rows, _ = e.status()
    same(rows["alpha-review"], ("review", "done", "shop", "clean, risk low"), "review that reported clean")


@check("wait: running, a report wins, a review's note")
def wait_checks(e):
    e.started("alpha")
    e.started("beta", new_id="abc12346")
    e.agents(abc12345="working", abc12346="working")
    t0 = time.time()
    rows, out = e.status(("wait", "--timeout", "0"))
    expect(time.time() - t0 < 8, "wait --timeout 0 slept instead of returning")
    same(out.splitlines()[0], "still running after 0s: alpha, beta", "first line while both run")
    same((rows["alpha"][1], rows["beta"][1]), ("running", "running"), "states while both run")

    e.report("beta", DONE)  # beta reported; the CLI still lists its session as working
    rows, out = e.status(("wait", "--timeout", "0"))
    same(out.splitlines()[0], "still running after 0s: alpha", "first line after beta reported")
    same(rows["beta"][1:], ("done", "shop", "Added the price column."), "beta after its report")
    rows, out = e.status(("wait", "beta", "--timeout", "0"))
    expect("still running" not in out, f"waiting for beta alone still reports a task running: {out!r}")
    same(sorted(rows), ["alpha", "beta"], "tasks listed after waiting for one name")
    rows, out = e.status(("wait", "alpha", "beta", "--timeout", "0"))
    same(out.splitlines()[0], "still running after 0s: alpha", "first line when both are named")

    e.agents(abc12345="done", abc12346="done")  # alpha stopped without a report: nothing to wait for
    t0 = time.time()
    rows, out = e.status(("wait",))
    expect(time.time() - t0 < 8, "wait slept although no session is working")
    expect("still running" not in out, f"wait reports a stopped session as running: {out!r}")
    same(rows["alpha"][1], "stopped-no-report", "alpha after its session stopped")

    e.reviewed("beta", new_id="abc12347")
    e.agents(abc12347="working")
    rows, out = e.status(("wait", "beta-review", "--timeout", "0"))
    same(out.splitlines()[0], "still running after 0s: beta-review", "first line while the review runs")
    e.report("beta-review", REVIEW_FINDINGS)
    rows, out = e.status(("wait", "beta-review", "--timeout", "0"))
    expect("still running" not in out, f"a review that reported is still waited for: {out!r}")
    same(rows["beta-review"], ("review", "done", "shop", "findings, risk high, 2 findings (1 major confirmed)"),
         "the review's line after wait")


# result, forget, read_result -----------------------------------------------------------


@check("result")
def result_checks(e):
    e.started("alpha")
    r = e.d("result", "alpha")
    same((r.returncode, r.stdout.strip()), (0, "no report yet; look with: claude logs abc12345"),
         "result before a report")
    e.report("alpha", DONE)
    r = e.d("result", "alpha")
    same((r.returncode, r.stdout), (0, DONE + "\n"), "result after a report")
    e.reviewed("alpha")
    e.report("alpha-review", REVIEW_FINDINGS)
    same(e.d("result", "alpha-review").stdout, REVIEW_FINDINGS + "\n", "result of a review")
    refused(e.d("result", "nobody"), "no task named nobody", "result of an unknown task")


@check("forget")
def forget_checks(e):
    e.started("alpha")
    e.started("beta", new_id="abc12346")
    e.report("alpha", DONE)
    calls = len(e.calls())
    r = e.d("forget", "alpha")
    same(r.returncode, 0, "exit code")
    has(r.stdout, "forgot alpha", "output")
    same([t["name"] for t in e.tasks()], ["beta"], "tasks after forget")
    same(len(e.calls()), calls, "claude calls made by forget (it must not touch the session)")
    expect((e.home / "results" / "alpha.md").read_text() == DONE, "forget changed the report file")
    expect((e.home / "briefs" / "alpha.md").exists(), "forget removed the brief file")
    rows, _ = e.status()
    same(sorted(rows), ["beta"], "status after forget")
    refused(e.d("forget", "alpha"), "no task named alpha", "forgetting it twice")
    refused(e.d("result", "alpha"), "no task named alpha", "result of a forgotten task")
    same([t["name"] for t in e.tasks()], ["beta"], "tasks after a refused forget")
    e.started("alpha", new_id="abc12347")
    same(e.task("alpha")["id"], "abc12347", "the name can be used again")
    expect(not (e.home / "results" / "alpha.md").exists(), "the old report survived the name being reused")


@check("read_result: a multi-line summary")
def read_result_checks(e):
    os.environ["DISPATCH_HOME"] = str(e.home)
    spec = importlib.util.spec_from_file_location("dispatch_under_test", DISPATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    same(mod.RESULTS, e.home / "results", "the results folder of the imported helper")
    head = "STATUS: done\nBRANCH: worktree-alpha\nPR: none\nTESTS: make test, passed\n"
    cases = (
        ("starts on the SUMMARY line",
         head + "SUMMARY: Added the price column.\nIt is empty for products with no price.\n\n"
                "The owner must decide the currency.\n",
         ("done", "Added the price column. It is empty for products with no price. "
                  "The owner must decide the currency.")),
        ("starts below the SUMMARY line",
         head + "SUMMARY:\n  Added the price column.\n  Nothing is left.\n",
         ("done", "Added the price column. Nothing is left.")),
        ("another field follows the summary",
         "STATUS: Failed\nSUMMARY: The build is broken.\nIt fails in shop/table.py.\n"
         "TESTS: make test, failed\nBRANCH: none\n",
         ("failed", "The build is broken. It fails in shop/table.py.")),
        ("one line", head + "SUMMARY: Added the price column.\n", ("done", "Added the price column.")),
        ("no summary", "STATUS: blocked\n", ("blocked", "")),
        ("no status", "I finished the work.\n", ("unknown", "")),
        ("a review with findings", REVIEW_FINDINGS,
         ("done", "findings, risk high, 2 findings (1 major confirmed)")),
        ("a clean review", REVIEW_CLEAN, ("done", "clean, risk low")),
    )
    same(mod.read_result("alpha"), (None, ""), "no report file")
    for what, text, want in cases:
        e.report("alpha", text)
        same(mod.read_result("alpha"), want, what)


# clean (uses the stand-in's `claude rm`) -----------------------------------------------


@check("clean: removes a finished task, refuses a running or uncommitted one")
def clean_checks(e):
    e.started("gamma")
    wt = e.repo / ".claude" / "worktrees" / "gamma"
    e.git("worktree", "add", "-q", "-b", "worktree-gamma", str(wt))
    tip = e.commit("gamma.txt", cwd=wt)
    (e.fake / "rm_removes").write_text(str(wt))

    def rms():
        return [c["args"] for c in e.calls() if c["args"][:1] == ["rm"]]

    e.agents(abc12345="working")
    refused(e.d("clean", "gamma"), "gamma is still running", "clean of a running task")
    e.agents(abc12345="done")
    (wt / "notes.txt").write_text("not committed\n")
    r = e.d("clean", "gamma")
    refused(r, "holds changes that are not committed", "clean with uncommitted work", code=6)
    has(r.stderr, "notes.txt", "the uncommitted file is named")
    same(rms(), [], "`claude rm` calls after two refusals")
    expect(wt.is_dir() and e.task("gamma"), "a refused clean removed something")
    refused(e.d("clean", "nobody"), "no task named nobody", "clean of an unknown task")

    (wt / "notes.txt").unlink()
    (wt / ".DS_Store").write_text("left by a tool\n")
    e.report("gamma", DONE)
    r = e.d("clean", "gamma")
    expect(r.returncode == 0, f"clean failed: {r.stderr.strip()}")
    same(r.stdout.strip(),
         "removed gamma: session and worktree gone; its commits are on the branch worktree-gamma", "output")
    same(rms(), [["rm", "abc12345"]], "`claude rm` calls")
    same(e.tasks(), [], "tasks after clean")
    same(e.git("rev-parse", "worktree-gamma"), tip, "the task's branch still holds its commit")
    same(e.git("branch", "--list", "kept/*"), "", "spare branches left behind")

    e.started("delta", new_id="abc12346")
    wt2 = e.repo / ".claude" / "worktrees" / "delta"
    e.git("worktree", "add", "-q", "-b", "worktree-delta", str(wt2))
    (e.fake / "rm_removes").write_text("")
    (e.fake / "rm_output").write_text("error: could not remove the worktree\n")
    r = e.d("clean", "delta")
    refused(r, "did not remove the worktree", "`claude rm` that leaves the worktree", code=6)
    has(r.stderr, "could not remove the worktree", "what claude printed is shown")
    same([t["name"] for t in e.tasks()], ["delta"], "tasks after a clean that failed")


# envs (a stand-in for the claude.ai API on 127.0.0.1) ------------------------------------


ENV_PAGES = {
    None: {"environments": [
        {"environment_id": "env_light1", "name": "Light", "kind": "anthropic_cloud", "state": "active"},
        {"environment_id": "env_vpn1", "name": "VPN", "kind": "anthropic_cloud", "state": "active"},
    ], "has_more": True, "last_id": "env_vpn1"},
    "env_vpn1": {"environments": [
        {"environment_id": "env_old1", "name": "Old", "kind": "anthropic_cloud", "state": "archived"},
    ], "has_more": False, "last_id": "env_old1"},
}


def fake_api(seen, sessions=None, events=None, files=None):
    import http.server
    import threading
    from urllib.parse import parse_qs, urlparse

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            seen.append((u.path, {k.lower(): v for k, v in self.headers.items()}))
            if self.headers.get("Authorization") != "Bearer tok-123":
                body, code = {"error": "unauthorized"}, 401
            elif u.path == "/v1/environment_providers":
                body, code = ENV_PAGES[parse_qs(u.query).get("after_id", [None])[0]], 200
            elif u.path == "/v1/code/sessions" and sessions is not None:
                rows = sorted(sessions.values(), key=lambda x: x["id"])
                cur = int(parse_qs(u.query).get("cursor", ["0"])[0])
                more = cur + 2 < len(rows)  # two to a page, so paging is exercised
                body, code = {"data": rows[cur:cur + 2],
                              "next_cursor": str(cur + 2) if more else None}, 200
            elif u.path.startswith("/api/oauth/files/") and files is not None:
                data = files.get(u.path.split("/")[4])
                code = 200 if data is not None else 404
                self.send_response(code)
                self.send_header("Content-Length", str(len(data or b"")))
                self.end_headers()
                self.wfile.write(data or b"")
                return
            elif u.path.endswith("/events") and events is not None:
                rows = events.get(u.path.split("/")[4], [])
                q = parse_qs(u.query)
                cur = int(q.get("cursor", ["0"])[0])
                desc = list(reversed(rows))  # newest first, as sort_order=desc asks
                more = cur + 3 < len(desc)  # three to a page, so paging is exercised
                body, code = {"data": [{"payload": x} for x in desc[cur:cur + 3]],
                              "next_cursor": str(cur + 3) if more else None}, 200
            elif u.path.startswith("/v1/code/sessions/") and sessions is not None:
                found = sessions.get(u.path.rsplit("/", 1)[1])
                body, code = ({"response_shape": found}, 200) if found else ({"error": "not found"}, 404)
            else:  # no self-hosted pools on this account
                body, code = {"error": "not found"}, 404
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@check("envs: lists the account's environments and resolves a name")
def envs_checks(e):
    seen = []
    srv = fake_api(seen)
    try:
        for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
            e.env.pop(k, None)
        e.env["DISPATCH_API_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
        refused(e.d("envs"), "no claude.ai login", "no login", code=6)

        (e.dir / ".claude").mkdir()
        creds = e.dir / ".claude" / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123",
                                                       "expiresAt": 1000}}))
        refused(e.d("envs"), "has expired", "an expired login", code=6)
        creds.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "tok-123", "expiresAt": int(time.time() + 3600) * 1000}}))
        refused(e.d("envs"), "no organization", "no organization", code=6)
        (e.dir / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationUuid": "org-9"}}))

        (e.home / "config.json").write_text(json.dumps({
            "cloud_environment": "light",
            "environments": {"light": "env_light1", "gone": "env_gone1"}}))
        r = e.d("envs")
        same(r.returncode, 0, f"envs exit code ({r.stderr.strip()})")
        lines = r.stdout.splitlines()
        has(lines[0], "'Light' id=env_light1", "first environment")
        has(lines[0], "config name: light; cloud_environment", "config roles")
        has(lines[2], "'Old' id=env_old1 kind=anthropic_cloud state=archived", "the second page")
        has(r.stdout, "'gone' points to env_gone1", "a config id the account lacks")
        expect("tok-123" not in r.stdout + r.stderr, "the token was printed")
        path, headers = seen[-3]
        same(headers.get("x-organization-uuid"), "org-9", "organization header")

        r = e.d("envs", "--json")
        same([x["id"] for x in json.loads(r.stdout)], ["env_light1", "env_vpn1", "env_old1"], "--json ids")

        os.environ["DISPATCH_HOME"] = str(e.home)
        saved = {k: os.environ.get(k) for k in ("HOME", "DISPATCH_API_URL", "CLAUDE_CONFIG_DIR",
                                                "CLAUDE_CODE_OAUTH_TOKEN")}
        os.environ.update(HOME=str(e.dir), DISPATCH_API_URL=e.env["DISPATCH_API_URL"])
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        try:
            spec = importlib.util.spec_from_file_location("dispatch_envs", DISPATCH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            class A:
                environment = None

            def pick(want, cfg=None):
                A.environment = want
                return mod.pick_environment(A(), cfg or {})[:2]

            same(pick("vpn"), ("vpn", "env_vpn1"), "a claude.ai name, any case")
            same(pick("light", {"environments": {"light": "env_x"}}), ("light", "env_x"),
                 "a config name wins over a claude.ai name")
            same(pick("env_direct"), ("env_direct", "env_direct"), "an id needs no lookup")
            for want in ("old", "nope"):  # archived, or not there
                try:
                    pick(want)
                    raise Failure(f"{want!r} was accepted")
                except SystemExit:
                    pass
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        srv.shutdown()


# cloud tasks read through the API `claude --teleport` uses (a stand-in on 127.0.0.1) ----


def cloud_session(sid, worker, category="", line="", needs="", unpushed=False, branch=None):
    return {"id": sid, "status": "active", "worker_status": worker, "status_line": line,
            "post_turn_summary": {"status_category": category, "needs_action": needs,
                                  "recent_action": "", "status_detail": line},
            "external_metadata": {"unpushed_work": unpushed, "usage": {"cost_usd": 1.5},
                                  "current_branches": {"": branch} if branch else {}}}


def cloud_task(name, n, started=None):
    return {"name": name, "where": "cloud", "id": f"session_0{n}", "repo": "",
            "branch": f"dispatch/{name}", "base": "main",
            "url": f"https://claude.ai/code/session_0{n}", "started": started or int(time.time())}


@check("cloud: status, wait and result read the session from claude.ai")
def cloud_checks(e):
    live = {}
    seen = []
    srv = fake_api(seen, live)
    try:
        for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
            e.env.pop(k, None)
        e.env["DISPATCH_API_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
        old = int(time.time()) - 3600
        tasks = [cloud_task(n, i, old) for i, n in enumerate(
            ("run", "ask", "fail", "stuck", "unpushed", "deleted"), 1)]
        for t in tasks:
            t["repo"] = str(e.repo)
        (e.home / "tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tasks))

        rows, out = e.status()  # no login: GitHub only, so after 20 minutes each is quiet
        same({r[1] for r in rows.values()}, {"quiet"}, "states without a claude.ai login")
        has(out, "note: claude.ai could not be read", "the note when claude.ai is not read")

        (e.dir / ".claude").mkdir()
        (e.dir / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "tok-123", "expiresAt": int(time.time() + 3600) * 1000}}))
        (e.dir / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationUuid": "org-9"}}))
        live.update({
            "cse_01": cloud_session("cse_01", "running", line="writing the tests"),
            "cse_02": cloud_session("cse_02", "requires_action", needs="approve the migration"),
            "cse_03": cloud_session("cse_03", "idle", "failed", line="API Error: 529 Overloaded"),
            "cse_04": cloud_session("cse_04", "idle", "blocked", needs="which currency?"),
            "cse_05": cloud_session("cse_05", "idle", "completed", line="price column added",
                                    unpushed=True, branch="claude/own-branch"),
        })
        rows, out = e.status()
        want = {
            "run": ("running", "writing the tests"),
            "ask": ("waiting-for-you", "approve the migration; open https://claude.ai/code/session_02"),
            "fail": ("failed", "API Error: 529 Overloaded; open https://claude.ai/code/session_03"),
            "stuck": ("blocked", "which currency?; open https://claude.ai/code/session_04"),
            "unpushed": ("not-pushed", "stopped, nothing on GitHub (unpushed work): price column added; "
                                       "open https://claude.ai/code/session_05"),
            "deleted": ("gone", "claude.ai no longer has this session, and nothing of it is on GitHub"),
        }
        for name, (state, note) in want.items():
            same((rows[name][1], rows[name][3]), (state, note), f"cloud task {name}")
        expect("note: claude.ai" not in out, f"a note although claude.ai was read: {out!r}")
        same({p for p, _ in seen if p.startswith("/v1/code/sessions/")},
             {f"/v1/code/sessions/cse_0{i}" for i in range(1, 7)}, "sessions asked for (session_ is cse_)")

        up = cloud_task("upped", 7, old)
        up.update(repo=str(e.repo), upload=True, branch=None)
        with open(e.home / "tasks.jsonl", "a") as fh:
            fh.write(json.dumps(up) + "\n")
        live["cse_07"] = cloud_session("cse_07", "idle", "completed", line="sub added, tests pass")
        rows, _ = e.status()
        same((rows["upped"][1], rows["upped"][3]),
             ("done", "sub added, tests pass; `pull upped` brings its files"), "an uploaded task that finished")
        refused(e.d("review", "upped"), "no GitHub branch", "review of an uploaded task")

        r = e.d("result", "unpushed")
        same(r.returncode, 0, f"result exit code ({r.stderr.strip()})")
        for part in ("STATUS: not-pushed", "BRANCH: claude/own-branch", "UNPUSHED:", "COST: $1.50",
                     "claude --teleport session_05"):
            has(r.stdout, part, "result of a cloud task")
        expect("tok-123" not in r.stdout + r.stderr, "the token was printed")

        rows, out = e.status(("wait", "run", "ask", "--timeout", "0"))
        same(out.splitlines()[0], "still running after 0s: run", "wait: only the running session is waited for")
        live["cse_01"] = cloud_session("cse_01", "idle", "completed", line="done")
        t0 = time.time()
        rows, out = e.status(("wait", "run"))
        expect(time.time() - t0 < 8, "wait slept although the cloud session is idle")
        expect("still running" not in out, f"wait reports an idle cloud session as running: {out!r}")
    finally:
        srv.shutdown()


@check("cloud: sessions lists the account's sessions, adopt tracks one")
def adopt_checks(e):
    live = {}
    srv = fake_api([], live)
    try:
        for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
            e.env.pop(k, None)
        e.env["DISPATCH_API_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
        (e.dir / ".claude").mkdir()
        (e.dir / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "tok-123", "expiresAt": int(time.time() + 3600) * 1000}}))
        (e.dir / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationUuid": "org-9"}}))
        src = {"sources": [{"type": "git_repository", "url": "https://github.com/acme/shop",
                            "revision": "main"}]}
        for i, (worker, cat, title) in enumerate((("running", "", "Price column"),
                                                  ("idle", "completed", "Fix login"),
                                                  ("idle", "failed", "Old idea"))):
            row = cloud_session(f"cse_0{i + 1}", worker, cat, line=f"line {i + 1}",
                                branch="claude/price" if i == 0 else None)
            row.update(title=title, config=src, created_at="2026-11-27T08:00:00Z")
            live[row["id"]] = row
        live["cse_03"]["status"] = "archived"

        r = e.d("sessions")
        same(r.returncode, 0, f"sessions exit code ({r.stderr.strip()})")
        lines = r.stdout.splitlines()
        same([l.split()[0] for l in lines], ["session_01", "session_02"], "sessions, archived left out")
        has(lines[0], "running", "state of a running session")
        has(lines[0], "acme/shop  'Price column'", "repository and title")
        r = e.d("sessions", "--all")
        same(len(r.stdout.splitlines()), 3, "sessions --all, over two pages")
        has(r.stdout.splitlines()[2], "archived", "an archived session")

        e.git("remote", "add", "origin", "https://github.com/acme/other.git")
        refused(e.d("adopt", "cse_01", "--name", "price", "--repo", e.repo),
                "works on acme/shop", "adopt with a checkout of another repository")
        e.git("remote", "set-url", "origin", "git@github.com:acme/shop.git")
        refused(e.d("adopt", "session_09", "--name", "price", "--repo", e.repo),
                "claude.ai has no session", "adopt of an unknown session")
        brief = e.dir / "brief.md"
        brief.write_text(BRIEF + "\n")
        r = e.d("adopt", "cse_01", "--name", "price", "--repo", e.repo, "--brief-file", brief)
        same(r.returncode, 0, f"adopt exit code ({r.stderr.strip()})")
        has(r.stdout, "adopted session_01 as price", "adopt output")
        t = e.task("price")
        same((t["where"], t["id"], t["branch"], t["base"], t["url"]),
             ("cloud", "session_01", "claude/price", "main", "https://claude.ai/code/session_01"),
             "the adopted task")
        same(e.brief_of("price"), BRIEF + "\n", "the adopted task's brief")
        refused(e.d("adopt", "session_01", "--name", "again", "--repo", e.repo),
                "already the task price", "adopting the same session twice")
        has(e.d("sessions").stdout.splitlines()[0], "(dispatch: price)", "sessions marks a tracked one")
    finally:
        srv.shutdown()


def use(uid, name, **inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": uid, "name": name, "input": inp}]}}


def res(uid, error=False, **tur):
    return {"type": "user", "tool_use_result": tur, "message": {"content": [
        {"type": "tool_result", "tool_use_id": uid, "is_error": error}]}}


@check("pull: rebuilds written files and downloads sent ones from a session's history")
def pull_checks(e):
    video = b"\x00\x00\x00 ftypisom" + b"x" * 5000
    history = [
        use("t1", "Write", file_path="/home/user/shop/new.py", content="a = 1\n"),
        res("t1"),
        use("t2", "Edit", file_path="/home/user/shop/new.py", old_string="a = 1", new_string="a = 2"),
        res("t2", originalFile="a = 1\n"),
        use("t3", "Edit", file_path="/home/user/shop/README.md", old_string="old", new_string="new"),
        res("t3", originalFile="the old text\n"),  # a file from the upload: its result has it
        use("t4", "Write", file_path="/home/user/shop/refused.txt", content="no"),
        res("t4", error=True),
        use("t5", "Edit", file_path="/home/user/shop/lost.txt", old_string="x", new_string="y"),
        res("t5"),  # no originalFile: nothing to start from
        use("t6", "SendUserFile", files=["/home/user/shop/out/v.mp4"]),
        res("t6", attachments=[{"file_uuid": "f-1", "path": "/home/user/shop/out/v.mp4", "size": len(video)}]),
        use("t7", "Write", file_path="/etc/../../escape.txt", content="no"),
        res("t7"),
    ]
    live = {"cse_01": cloud_session("cse_01", "idle", "completed")}
    srv = fake_api([], live, {"cse_01": history}, {"f-1": video})
    try:
        for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
            e.env.pop(k, None)
        e.env["DISPATCH_API_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
        (e.dir / ".claude").mkdir()
        (e.dir / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "tok-123", "expiresAt": int(time.time() + 3600) * 1000}}))
        (e.dir / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationUuid": "org-9"}}))
        out = e.dir / "pulled"
        r = e.d("pull", "session_01", "--out", out)
        same(r.returncode, 0, f"pull exit code ({r.stderr.strip()})")
        same((out / "shop" / "new.py").read_text(), "a = 2\n", "a written then edited file")
        same((out / "shop" / "README.md").read_text(), "the new text\n", "an edited file from the upload")
        same((out / "shop" / "out" / "v.mp4").read_bytes(), video, "a sent file, byte for byte")
        expect(not (out / "shop" / "refused.txt").exists(), "a refused Write was rebuilt")
        expect(not (out / "shop" / "lost.txt").exists(), "an edit with no starting text was rebuilt")
        expect(not (e.dir / "escape.txt").exists(), "a path left the folder")
        listing = (out / "PULLED.md").read_text()
        has(listing, "skipped  /home/user/shop/lost.txt", "PULLED.md names the skipped file")
        has(r.stdout, "pulled 3 files", "pull output")
        refused(e.d("pull", "session_01", "--out", out), "is not empty", "pull into a full folder")
        r = e.d("pull", "session_01", "--out", out, "--into-existing")
        same(r.returncode, 0, "pull --into-existing")
    finally:
        srv.shutdown()


def main():
    only = sys.argv[1:]
    chosen = [(n, f) for n, f in CHECKS if not only or any(o in n for o in only)]
    if not chosen:
        print(f"no check matches {' '.join(only)}")
        return 1
    if not shutil.which("git"):
        print("FAIL  git is not on PATH; the checks need it for their temporary repositories")
        return 1
    failed = 0
    root = Path(tempfile.mkdtemp(prefix="dispatch-selftest-")).resolve()
    saved = os.environ.get("DISPATCH_HOME")
    try:
        for i, (name, fn) in enumerate(chosen):
            try:
                fn(Env(root, f"c{i:02d}"))
                print(f"ok    {name}")
            except Failure as x:
                failed += 1
                print(f"FAIL  {name}: {x}")
            except Exception as x:  # a check that crashes is a failed check, not a lost run
                failed += 1
                print(f"FAIL  {name}: {type(x).__name__}: {x}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if saved is None:
            os.environ.pop("DISPATCH_HOME", None)
        else:
            os.environ["DISPATCH_HOME"] = saved
    n = len(chosen)
    print(f"{n} checks: {n - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
