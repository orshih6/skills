#!/usr/bin/env python3
"""dispatch: start Claude Code sessions (local background or cloud) and track them.

Standard library only. State lives in ~/.claude/dispatch/ (never in a repo):

  config.json        optional local settings (see SKILL.md)
  tasks.jsonl        one line per dispatched task
  results/<name>.md  what each local session reports when it stops
  briefs/<name>.md   the exact brief each session was given

A review is a local task named <name>-review: a new session that did not write the
change reads the finished task's branch and reports a verdict.

Commands: local, cloud, review, wait, status, envs, sessions, adopt, result, clean, forget. Run with -h for flags.
"""

import argparse
import hashlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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
{push}
- Do not ask questions and wait: nobody will answer. If you need a decision, report
  `blocked` with the question.
- Before you report `done`, run the project's tests or build and say what happened.
{extra}"""

LOCAL_PUSH = """\
- Commit your work to `{branch}` and leave it on this machine. Do not push the branch
  and do not open a pull request unless the task above asks for one, even if the
  project's own instructions say to push finished work: another session reviews the
  branch first, and the owner decides what leaves the machine."""

CLOUD_PUSH = "- Push only the branch named above."

LOCAL_REPORT = """\
## Reporting (required, last step)

Write your result to `{result}` in exactly this shape, then stop:

    STATUS: done | failed | blocked
    BRANCH: <branch name, or none>
    PR: <url, or none (none unless the task asked for a pull request)>
    TESTS: <command you ran and whether it passed, or not run and why>
    SUMMARY: <2 to 5 lines: what changed, what is left, anything the owner must decide>

If you are told to hand off, write the same file with `STATUS: handoff` and put what is
done, what is left and what you were about to try under SUMMARY, commit your work in
progress to `{branch}`, push that branch, and stop.
{notify}"""

CLOUD_REPORT = """\
## Reporting (required, last step)

Push the branch `{branch}` and open a pull request from it (draft if unfinished). If
the push of that branch is refused because this session may only push its own branch,
move your commits to the session's own branch, push that one, and open the pull
request from it. Start the pull request description with these lines:

    DISPATCH: {name}
    STATUS: done | failed | blocked
    TESTS: <command you ran and whether it passed, or not run and why>
    SUMMARY: <2 to 5 lines: what changed, what is left, anything the owner must decide>

If you cannot open a pull request, push the branch and add the same text as
`DISPATCH_RESULT.md` in its last commit.

If you cannot push at all (the repository refuses, or GitHub access is missing), your
work cannot leave this machine and the owner will see nothing. Do not spend time on
workarounds: say plainly in your last message that the push failed, why, and what the
owner must do (usually: install the Claude GitHub App on the repository with write
access), then stop. The owner can then tell this session to push again.
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
    1. MAJOR | MINOR  CONFIRMED | PLAUSIBLE  <file>:<line>  <what goes wrong, and with which input>

Write `FINDINGS: none` when there are none. A finding is MAJOR when the change does not
do something the task calls done, or breaks something that worked before; everything
else is MINOR. Use `VERDICT: clean` when there are no findings. RISK is about what the change touches, not
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
    for t in tasks:  # "model" and "environment" are only written when one was chosen
        for k in ("model", "environment"):
            if t.get(k) is None:
                t.pop(k, None)
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


def hide_worktrees(repo):
    """List .claude/worktrees/ in the repo's own exclude file (local, never committed)."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return
    f = Path(common) / "info" / "exclude"
    line = ".claude/worktrees/"
    old = f.read_text() if f.exists() else ""
    if line not in old.splitlines():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(old + ("" if old.endswith("\n") or not old else "\n") + line + "\n")


def pick_model(args, cfg, where):
    """--model wins, then the config's cloud_model or local_model, then the account default."""
    return getattr(args, "model", None) or cfg.get(f"{where}_model") or None


def claude_config_dir():
    """Where the claude CLI keeps its login: CLAUDE_CONFIG_DIR, else ~/.claude."""
    return os.environ.get("CLAUDE_CONFIG_DIR")


class LoginMissing(Exception):
    pass


def claude_login(soft=False):
    """(access token, organization uuid) of the claude CLI's claude.ai login.

    The same login `claude --cloud` uses, read where the CLI keeps it: the
    CLAUDE_CODE_OAUTH_TOKEN variable, `.credentials.json` in its config folder, or the
    macOS Keychain. Never printed and never written anywhere. Without a usable login it
    stops with an error, or with soft raises LoginMissing.
    """
    try:
        return read_login()
    except LoginMissing as e:
        if soft:
            raise
        die(str(e), 6)


def read_login():
    custom = claude_config_dir()
    folder = Path(custom) if custom else Path.home() / ".claude"
    token, expires = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), None
    if not token:
        raw = None
        creds = folder / ".credentials.json"
        if creds.exists():
            raw = creds.read_text()
        elif sys.platform == "darwin" and shutil.which("security"):
            service = "Claude Code-credentials"
            if custom:  # each config folder has its own Keychain item
                service += "-" + hashlib.sha256(custom.encode()).hexdigest()[:8]
            r = run(["security", "find-generic-password", "-s", service, "-w"])
            raw = r.stdout if r.returncode == 0 else None
        try:
            oauth = json.loads(raw or "{}").get("claudeAiOauth") or {}
        except json.JSONDecodeError:
            oauth = {}
        token, expires = oauth.get("accessToken"), oauth.get("expiresAt")
    if not token:
        raise LoginMissing("no claude.ai login found for the claude CLI. Run `claude` and "
                           "sign in with your subscription (/login), then try again.")
    if expires and expires / 1000 < time.time():
        raise LoginMissing("the claude CLI's login has expired. Start `claude` once so it "
                           "renews the login, then try again.")
    state = (Path(custom) if custom else Path.home()) / ".claude.json"
    try:
        org = json.loads(state.read_text()).get("oauthAccount", {}).get("organizationUuid")
    except (OSError, json.JSONDecodeError):
        org = None
    if not org:
        raise LoginMissing(f"no organization found in {state}. Run `claude` and sign in, "
                           "then try again.")
    return token, org


def api_call(path, token, org, extra_headers=None):
    """GET a claude.ai API path: (HTTP status, JSON body). Status 0 when it was not reached."""
    url = os.environ.get("DISPATCH_API_URL", "https://api.anthropic.com") + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "x-organization-uuid": org,
        "anthropic-version": "2023-06-01", "Content-Type": "application/json",
        **(extra_headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return 0, str(e)


def api_get(path, token, org, extra_headers=None, optional=False):
    """GET a claude.ai API path as JSON. Stops with an error, or gives None when optional."""
    code, body = api_call(path, token, org, extra_headers)
    if code == 200:
        return body
    if optional:
        return None
    if code:
        die(f"GET {path} failed: HTTP {code}. If it is 401, start `claude` once so it "
            "renews the login.", 6)
    die(f"GET {path} failed: {body}", 6)


GONE = "gone"


class CloudSessions:
    """What claude.ai knows about cloud sessions, read through the API `claude --teleport`
    uses (GET /v1/code/sessions/<id>). Not documented; it may change with a CLI update.

    get() gives the session's fields, GONE when claude.ai no longer has it, or None when
    nothing could be read (no login, no network, an unknown answer). Each session is read
    once per run of the helper; `fresh` reads again (for `wait`).
    """

    def __init__(self):
        self.login, self.cache, self.problem = None, {}, None

    def get(self, sid, fresh=False):
        if not sid:
            return None
        if sid in self.cache and not fresh:
            return self.cache[sid]
        if self.login is None:
            try:
                self.login = claude_login(soft=True)
            except LoginMissing as e:
                self.login, self.problem = False, str(e)
        if not self.login:
            return None
        cse = "cse_" + sid[len("session_"):] if sid.startswith("session_") else sid
        code, body = api_call(f"/v1/code/sessions/{urllib.parse.quote(cse)}", *self.login)
        if code == 404:
            found = GONE
        elif code == 200 and isinstance(body, dict):
            s = body.get("response_shape") or body.get("session") or body
            found = s if isinstance(s, dict) and s.get("id") else None
        else:
            found = None
        if found is None:
            self.problem = f"HTTP {code}" if code else (body or "no answer")
        self.cache[sid] = found
        return found


def session_facts(s):
    """The fields of a cloud session that dispatch uses, with defaults."""
    meta = s.get("external_metadata") or {}
    summary = s.get("post_turn_summary") or meta.get("post_turn_summary") or {}
    branches = [b for b in (meta.get("current_branches") or {}).values() if b]
    return {
        "archived": s.get("status") == "archived",
        "worker": s.get("worker_status") or "",
        "category": summary.get("status_category") or s.get("status_bucket") or "",
        "line": " ".join((s.get("status_line") or summary.get("status_detail") or "").split()),
        "recent": " ".join((summary.get("recent_action") or "").split()),
        "needs": " ".join((summary.get("needs_action") or "").split()),
        "branch": branches[0] if branches else None,
        "unpushed": meta.get("unpushed_work") is True,
        "cost": (meta.get("usage") or {}).get("cost_usd"),
    }


def cloud_environments():
    """The account's cloud environments and self-hosted pools, as claude.ai/code lists them.

    Each is {"id", "name", "kind", "state", "description"}. This is the API that
    `/remote-env` in the CLI calls. It is not documented and may change with a CLI update.
    """
    token, org = claude_login()
    found, after = [], None
    for _ in range(20):
        q = "?limit=100" + (f"&after_id={urllib.parse.quote(after)}" if after else "")
        page = api_get("/v1/environment_providers" + q, token, org)
        envs = page.get("environments") or []
        found += [{"id": e.get("environment_id"), "name": e.get("name") or "",
                   "kind": e.get("kind") or "", "state": e.get("state") or "",
                   "description": e.get("description") or ""} for e in envs]
        after = page.get("last_id")
        if not page.get("has_more") or not envs or not after:
            break
    # self-hosted pools are optional; an account without them may refuse the call
    pools = (api_get("/v1/code/runners/self-hosted/pools", token, org,
                     {"anthropic-beta": "ccr-byoc-2025-07-29"}, optional=True) or {}).get("pools") or []
    found += [{"id": p.get("pool_id"), "name": p.get("name") or "", "kind": "self_hosted_pool",
               "state": f"{p.get('alive_runner_count', 0)} runners", "description": ""}
              for p in pools]
    return [e for e in found if e["id"]]


def pick_environment(args, cfg):
    """The cloud environment for a task, as (label, id, extra claude arguments).

    --environment wins, then the config's cloud_environment. Either may be a name from
    the config's `environments` map, an id, or the name the environment has on
    claude.ai/code (looked up live). Nothing chosen: the account's default.
    """
    want = getattr(args, "environment", None) or cfg.get("cloud_environment")
    if not want:
        return None, None, []
    envs = cfg.get("environments", {})
    env_id = envs.get(want, want)
    if not env_id.startswith(("env_", "ccpool_")):
        live = [e for e in cloud_environments() if e["state"] != "archived"]
        hits = [e for e in live if e["name"].casefold() == want.casefold()]
        if len(hits) != 1:
            names = sorted(set(envs) | {e["name"] for e in live})
            die(f"{'several environments are' if hits else 'no environment is'} named "
                f"{want!r}. Use an id (env_... or ccpool_...) or one of: "
                f"{', '.join(repr(n) for n in names) or 'none'}. `envs` lists them.")
        env_id = hits[0]["id"]
    if env_id.startswith("ccpool_"):  # a self-hosted environment has its own flag
        return want, env_id, ["--environment", env_id]
    # a hosted environment is chosen through the setting the CLI reads its default from
    return want, env_id, ["--settings", json.dumps({"remote": {"defaultEnvironmentId": env_id}})]


def start_bg(repo, name, prompt, worktree, model=None):
    """Start a local background session and return its short id."""
    cmd = ["claude", "--bg", "-n", name]
    if model:
        cmd += ["--model", model]
    if worktree:
        hide_worktrees(repo)
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
        RULES.format(branch=branch, extra=extra, push=LOCAL_PUSH.format(branch=branch)),
        LOCAL_REPORT.format(result=result, branch=branch, notify=notify),
    ])
    for d in (RESULTS, BRIEFS):
        d.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{args.name}.md").write_text(full + "\n")
    result.unlink(missing_ok=True)

    base = git(repo, "rev-parse", "HEAD") if is_git else None
    model = pick_model(args, cfg, "local")
    task = {
        "name": args.name, "where": "local", "model": model,
        "id": start_bg(repo, args.name, full, use_wt, model),
        "repo": str(repo), "branch": branch if is_git else None, "base": base or None,
        "worktree": use_wt, "needs_local": bool(args.needs_local),
        "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started local {args.name} id={task['id']} repo={repo.name} branch={task['branch']}")
    if use_wt:
        ahead = git(repo, "log", "--oneline", "@{u}..HEAD").splitlines()
        if ahead or not git(repo, "rev-parse", "--abbrev-ref", "@{u}"):
            what = f"{len(ahead)} unpushed commit{'s' if len(ahead) != 1 else ''}" if ahead else "a branch with no upstream"
            print(f"note: this repo has {what}. A worktree can start from the pushed state, so the "
                  "session may not see that work. If the task depends on it, stop the session, "
                  "then push first or use --no-worktree.")


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
        RULES.format(branch=branch, extra=cfg.get("extra_rules", ""), push=CLOUD_PUSH),
        CLOUD_REPORT.format(branch=branch, name=args.name),
    ])
    BRIEFS.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{args.name}.md").write_text(full + "\n")

    model = pick_model(args, cfg, "cloud")
    env_name, env_id, env_args = pick_environment(args, cfg)
    out = spawn_in_pty(["claude", "--cloud", full, "-n", args.name]
                       + (["--model", model] if model else []) + env_args, cwd=repo)
    if "trust this folder" in out:
        die(f"{repo} is not a trusted folder. The owner must run `claude` there once "
            "and accept the trust prompt. Do not accept it for them.", 3)
    m = re.search(r"(session_[A-Za-z0-9]+)", out)
    if "Created cloud session" not in out or not m:
        die("could not create the cloud session:\n" + out.strip()[-600:])
    url = re.search(r"https://claude\.ai/code/\S+", out)
    task = {
        "name": args.name, "where": "cloud", "model": model, "environment": env_id,
        "id": m.group(1), "repo": str(repo),
        "branch": branch, "base": args.base or git(repo, "branch", "--show-current") or None,
        "url": url.group(0).split("?")[0] if url else None,
        "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started cloud {args.name} id={task['id']} repo={repo.name} branch={branch}"
          + (f" model={model}" if model else "")
          + f" environment={env_name or 'account default'}")
    print(f"view: {task['url']}")


def resolves(repo, ref):
    return bool(ref and git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}"))


def review_range(t, repo):
    """The two commits a review compares: where the task's branch left its base, and its tip."""
    if not t.get("branch"):
        die(f"{t['name']} has no branch "
            + ("(claude.ai names none for this session yet)" if t["where"] == "cloud"
               else "(its folder is not a git repository)") + "; there is nothing to diff")
    cloud = t["where"] == "cloud"
    if cloud:
        s = CloudSessions().get(t["id"])
        own = session_facts(s)["branch"] if isinstance(s, dict) else None
        pr = cloud_pr(t, own)
        if pr and pr["headRefName"] != t["branch"]:
            t["branch"] = pr["headRefName"]  # the session had to push its own branch
        elif own and own != t["branch"] and not git(repo, "ls-remote", "--heads", "origin", t["branch"]):
            t["branch"] = own
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
        die(f"the brief of {args.name} is missing ({bf}); a review needs to know what was asked"
            + (". Write what the session was asked to a file and `adopt` it again with "
               "--brief-file (forget it first)" if t.get("adopted") else ""))
    extra = cfg.get("extra_rules", "")
    result = RESULTS / f"{name}.md"
    full = REVIEW.format(brief=brief, base=base, ref=ref, result=result,
                         extra=extra + "\n" if extra else "", notify=notify_text(cfg, name))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{name}.md").write_text(full + "\n")
    result.unlink(missing_ok=True)
    model = pick_model(args, cfg, "local")
    task = {
        "name": name, "where": "local", "kind": "review", "of": args.name, "model": model,
        "id": start_bg(repo, name, full, True, model), "repo": str(repo), "branch": None,
        "worktree": True, "needs_local": True, "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started review {name} id={task['id']} repo={repo.name} diff={base}..{ref}")


def cmd_wait(args):
    """Block until the named tasks (default: all of them) have reported or stopped.

    A local task is waited for while its session works and has written no report; a cloud
    task while claude.ai says its session is running. A cloud task whose state cannot be
    read is not waited for.
    """
    tasks = [t for t in load_tasks() if not args.names or t["name"] in args.names]
    cs = CloudSessions()
    end = time.time() + args.timeout
    while True:
        live = sessions() if any(t["where"] == "local" for t in tasks) else {}
        left = []
        for t in tasks:
            if t["where"] == "cloud":
                s = cs.get(t["id"], fresh=True)
                busy = isinstance(s, dict) and session_facts(s)["worker"] == "running"
            else:
                busy = (not (RESULTS / f"{t['name']}.md").exists()
                        and live.get(t["id"], {}).get("state") == "working")
            if busy:
                left.append(t["name"])
        if not left or time.time() >= end:
            break
        time.sleep(10)
    if left:
        print(f"still running after {args.timeout}s: {', '.join(left)}")
    cmd_status(args, cs)


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
        major = len(re.findall(r"^\s*\d+\.\s+MAJOR\s+CONFIRMED", text, re.M))
        note = f"{v.group(1).lower()}, risk {r.group(1).lower() if r else 'unknown'}"
        if n:
            note += f", {n} finding{'s' if n != 1 else ''} ({major} major confirmed)"
        return status, note
    # The summary is 2 to 5 lines: it may start on the SUMMARY: line or below it, and
    # runs to the end of the report, or to another report field if one follows.
    s = re.search(r"^SUMMARY:(.*?)(?=^(?:STATUS|BRANCH|PR|TESTS):|\Z)", text, re.M | re.S)
    return status, (" ".join(s.group(1).split()) if s else "")


def cloud_pr(t, own=None):
    """The task's pull request: by its branch or the session's own branch (`own`, from
    claude.ai), else by the DISPATCH line in the body (a cloud session that may only push
    its own branch opens it from that one)."""
    if not shutil.which("gh"):
        return None
    fields = "url,state,isDraft,body,headRefName"
    heads = [["--head", b] for b in dict.fromkeys([t["branch"], own]) if b]
    for find in heads + [["--search", f'"DISPATCH: {t["name"]}" in:body']]:
        r = run(["gh", "pr", "list", *find, "--state", "all", "--json", fields, "--limit", "5"],
                cwd=t["repo"])
        if r.returncode != 0 or r.stdout.strip() in ("", "[]"):
            continue
        for pr in json.loads(r.stdout):
            if find[0] == "--head" or re.search(rf"^\s*DISPATCH:\s*{re.escape(t['name'])}\s*$",
                                                pr.get("body") or "", re.M):
                return pr
    return None


def cloud_state(t, cs=None):
    """(state, note) of a cloud task.

    Its own report in the pull request wins. Then what claude.ai says the session is
    doing (running, or stopped on a question). Then what reached GitHub. Then why
    nothing did: claude.ai says whether the session failed, is blocked, or stopped with
    its work unpushed. Without claude.ai (no login, no network) only GitHub is read, as
    `running-or-no-output`, which status turns into `quiet` after 20 minutes.
    """
    repo, url = t["repo"], t.get("url") or ""
    s = (cs or CloudSessions()).get(t["id"])
    f = session_facts(s) if isinstance(s, dict) else None
    pr = cloud_pr(t, f and f["branch"])
    if pr:
        m = re.search(r"STATUS:\s*(\w+)", pr.get("body") or "")
        if m:
            return m.group(1).lower(), pr["url"]
    if f and f["worker"] == "running":
        return "running", f["line"] or f["recent"]
    if f and f["worker"] == "requires_action":
        return "waiting-for-you", f"{f['needs'] or f['line'] or 'it waits for an answer'}; open {url}"
    if pr:
        return ("draft" if pr["isDraft"] else "pr-open"), pr["url"]
    for b in dict.fromkeys([t["branch"], f and f["branch"]]):
        if b and git(repo, "ls-remote", "--heads", "origin", b):
            return "branch-pushed", f"branch {b} (no pull request yet)"
    if s == GONE:
        return "gone", "claude.ai no longer has this session, and nothing of it is on GitHub"
    if not f:
        return "running-or-no-output", url
    line = f["line"] or f["recent"]
    if f["category"] == "failed":
        return "failed", f"{line}; open {url}"
    if f["category"] in ("blocked", "waiting"):
        return "blocked", f"{f['needs'] or line}; open {url}"
    if f["archived"]:
        return "archived", f"archived on claude.ai with nothing on GitHub; {line}"
    return "not-pushed", f"stopped, nothing on GitHub{' (unpushed work)' if f['unpushed'] else ''}: {line}; open {url}"


def cmd_status(args, cs=None):
    tasks = load_tasks()
    cs = cs or CloudSessions()
    only = getattr(args, "repo", None)
    if only:
        only = str(Path(only).expanduser().resolve())
        tasks = [t for t in tasks if t["repo"] == only]
    if not tasks:
        print("no dispatched tasks" + (f" in {only}" if only else ""))
        return
    live = sessions()
    for t in tasks:
        age = int((time.time() - t["started"]) / 60)
        if t["where"] == "cloud":
            state, note = cloud_state(t, cs)
            if state == "running-or-no-output" and age >= 20:
                # nothing can be read from a cloud session here; only GitHub shows its work
                state, note = "quiet", f"nothing on GitHub after {age}m; stuck or unable to push, open {note}"
        else:
            s = live.get(t["id"])
            rstat, summary = read_result(t["name"])
            cli = s.get("state") if s else "gone"
            # A report means the task is over, even while the CLI still lists the session
            # as working (it is only wrapping up). Then: working, blocked (waiting for a
            # person), done (stopped).
            if rstat:
                state, note = rstat, summary
            elif cli == "working":
                state, note = "running", ""
            elif cli == "blocked":
                state, note = "waiting-for-you", f"claude attach {t['id']}"
            elif cli == "done":
                state, note = "stopped-no-report", f"claude logs {t['id']}"
            else:
                state, note = "gone", "session no longer listed and left no report"
        where = "review" if t.get("kind") == "review" else t["where"]
        print(f"{t['name']:<28} {where:<6} {state:<20} {age:>4}m  {Path(t['repo']).name}  {note if 'https://' in note or len(note) <= 90 else note[:89] + '…'}")
    if cs.problem and any(t["where"] == "cloud" for t in tasks):
        print(f"note: claude.ai could not be read ({cs.problem}); cloud states come from GitHub only")


def cmd_result(args):
    t = next((t for t in load_tasks() if t["name"] == args.name), None)
    if not t:
        die(f"no task named {args.name}")
    if t["where"] == "cloud":
        cs = CloudSessions()
        state, note = cloud_state(t, cs)
        s = cs.get(t["id"])
        lines = [f"STATUS: {state}", note]
        if isinstance(s, dict):  # the session's own words, as claude.ai shows them
            f = session_facts(s)
            for label, key in (("NOW", "line"), ("LAST", "recent"), ("NEEDS", "needs"), ("BRANCH", "branch")):
                if f[key] and f[key] not in note:
                    lines.append(f"{label}: {f[key]}")
            if f["unpushed"]:
                lines.append("UNPUSHED: the session holds work that is not on GitHub")
            if isinstance(f["cost"], (int, float)):
                lines.append(f"COST: ${f['cost']:.2f}")
        elif cs.problem:
            lines.append(f"note: claude.ai could not be read ({cs.problem})")
        lines += [f"view: {t.get('url')}", f"bring it to this computer: claude --teleport {t['id']}"]
        print("\n".join(lines))
        return
    f = RESULTS / f"{args.name}.md"
    print(f.read_text() if f.exists() else f"no report yet; look with: claude logs {t['id']}")


JUNK = (".serena/", "__pycache__/", ".DS_Store", ".pyc", ".pytest_cache/", ".mypy_cache/")


def cmd_clean(args):
    """Remove a finished local task: its session, its worktree and its line in the list."""
    tasks = load_tasks()
    t = next((t for t in tasks if t["name"] == args.name), None)
    if not t:
        die(f"no task named {args.name}")
    if t["where"] != "local":
        die(f"{args.name} is a cloud task; there is nothing on this machine to remove. Use `forget`.")
    if sessions().get(t["id"], {}).get("state") == "working" and not (RESULTS / f"{args.name}.md").exists():
        die(f"{args.name} is still running; `claude stop {t['id']}` first if you mean it")
    wt = Path(t["repo"]) / ".claude" / "worktrees" / args.name
    if t.get("worktree") and wt.is_dir():
        junk = JUNK + tuple(load_config().get("leftovers", []))
        left = [l[3:] for l in git(wt, "status", "--porcelain").splitlines()]
        real = [f for f in left if not f.endswith(junk)]
        if real:
            die(f"{wt} holds changes that are not committed:\n  " + "\n  ".join(real)
                + "\nAsk the owner what to do with them. Nothing was removed.", 6)
        for f in left:  # files that tools leave behind, never the task's work
            target = wt / f
            shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
    # `claude rm` refuses while the worktree holds commits that are on no remote, which
    # is every local task that did what it was told and did not push. Make sure another
    # branch holds those commits, and only then let it discard the worktree.
    repo = t["repo"]
    has_wt = t.get("worktree") and wt.is_dir()
    head = git(wt, "rev-parse", "HEAD") if has_wt else ""
    own = git(wt, "branch", "--show-current") if has_wt else ""

    def holders():
        refs = git(repo, "branch", "-a", "--contains", head, "--format=%(refname:short)").splitlines()
        return [r for r in refs if r and r != own and not r.startswith("(")]

    keep = ""
    if head and not holders():
        keep = f"kept/{args.name}"
        run(["git", "-C", repo, "branch", "-f", keep, head])
    r = run(["claude", "rm", t["id"]], timeout=120)
    out = ANSI.sub("", r.stdout + r.stderr).strip()
    m = re.search(r"--discard-unpushed\s+(\S+)", out)
    if wt.is_dir() and m and head and holders():
        r = run(["claude", "rm", t["id"], "--discard-unpushed", m.group(1)], timeout=120)
        out = ANSI.sub("", r.stdout + r.stderr).strip()
    if wt.is_dir():
        die(f"`claude rm {t['id']}` did not remove the worktree:\n{out[-600:]}", 6)
    save_tasks([x for x in tasks if x["name"] != args.name])
    if own and resolves(repo, own) and git(repo, "rev-parse", own) == head:
        if keep:  # the task's own branch survived, the spare one is not needed
            run(["git", "-C", repo, "branch", "-D", keep])
        keep = own
    if t.get("kind") == "review":
        # a review's own branch never has commits; -d refuses if it somehow does
        run(["git", "-C", repo, "branch", "-d", f"worktree-{args.name}"])
        keep = ""
    note = f"; its commits are on the branch {keep}" if keep else ""
    print(f"removed {args.name}: session and worktree gone{note}")


def cmd_forget(args):
    tasks = load_tasks()
    keep = [t for t in tasks if t["name"] != args.name]
    if len(keep) == len(tasks):
        die(f"no task named {args.name}")
    save_tasks(keep)
    print(f"forgot {args.name} (the session, its branch and its report file are untouched)")


def session_id(sid):
    """The id as `claude --teleport` and claude.ai links take it: session_..., not cse_..."""
    return "session_" + sid[len("cse_"):] if sid.startswith("cse_") else sid


def session_repo(s):
    """owner/name of the GitHub repository a cloud session works on, or None."""
    for src in (s.get("config") or {}).get("sources") or []:
        m = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", src.get("url") or "")
        if src.get("type") == "git_repository" and m:
            return m.group(1)
    return None


def session_word(s):
    """One word for a cloud session's state, from what claude.ai says."""
    f = session_facts(s)
    if f["archived"]:
        return "archived"
    if f["worker"] == "running":
        return "running"
    if f["worker"] == "requires_action":
        return "waiting-for-you"
    return f["category"] or f["worker"] or "unknown"


def cmd_sessions(args):
    """Every cloud session of the account, newest first: the list `claude --teleport` shows."""
    token, org = claude_login()
    found, cursor = [], None
    for _ in range(20):
        q = "?limit=50" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        page = api_get("/v1/code/sessions" + q, token, org)
        rows = page.get("data") or []
        found += [r for r in rows if args.all or r.get("status") != "archived"]
        cursor = page.get("next_cursor")
        if len(found) >= args.limit or not rows or not cursor:
            break
    found = found[:args.limit]
    if args.json:
        print(json.dumps(found, indent=2))
        return
    if not found:
        print("no cloud sessions" + ("" if args.all else " (--all also lists archived ones)"))
        return
    mine = {t["id"]: t["name"] for t in load_tasks() if t["where"] == "cloud"}
    for r in found:
        sid = session_id(r.get("id", ""))
        f = session_facts(r)
        when = (r.get("last_event_at") or r.get("created_at") or "")[:16].replace("T", " ")
        title = " ".join((r.get("title") or "untitled").split())[:40]
        tag = f"  (dispatch: {mine[sid]})" if sid in mine else ""
        print(f"{sid}  {session_word(r):<15} {when}  {session_repo(r) or '-'}  {title!r}"
              f"{tag}  {f['line'][:80]}")


def cmd_adopt(args):
    """Track a cloud session that dispatch did not start, like one it did."""
    tasks = load_tasks()
    if not NAME_OK.match(args.name):
        die("name must be lowercase letters, digits and dashes (2 to 41 characters)")
    if any(t["name"] == args.name for t in tasks):
        die(f"a task named {args.name} already exists; pick another name or `forget` it")
    sid = session_id(args.session)
    if not sid.startswith("session_"):
        die("give the session's id (session_... or cse_...); `sessions` lists them")
    had = next((t["name"] for t in tasks if t["id"] == sid), None)
    if had:
        die(f"{sid} is already the task {had}")
    s = CloudSessions().get(sid)
    if s == GONE:
        die(f"claude.ai has no session {sid}; `sessions` lists them")
    if not isinstance(s, dict):
        die("could not read the session from claude.ai; is the claude CLI signed in?", 6)
    repo = Path(args.repo).expanduser().resolve()
    want = session_repo(s)
    have = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", git(repo, "remote", "get-url", "origin"))
    if not want:
        die(f"{sid} works on no GitHub repository; there is nothing dispatch can follow")
    if not have or have.group(1).lower() != want.lower():
        die(f"{sid} works on {want}, but {repo} is "
            + (f"a checkout of {have.group(1)}" if have else "not a GitHub checkout")
            + ". Give --repo a checkout of " + want)
    f = session_facts(s)
    outcome = [b for o in (s.get("config") or {}).get("outcomes") or []
               for b in (o.get("git_info") or {}).get("branches") or []]
    branch = f["branch"] or (outcome[0] if outcome else None)
    base = next((src.get("revision") for src in (s.get("config") or {}).get("sources") or []
                 if src.get("revision")), None)
    if args.brief_file:
        brief = Path(args.brief_file).read_text().strip()
        if not brief:
            die("the brief is empty")
        BRIEFS.mkdir(parents=True, exist_ok=True)
        (BRIEFS / f"{args.name}.md").write_text(brief + "\n")
    task = {
        "name": args.name, "where": "cloud", "adopted": True, "id": sid, "repo": str(repo),
        "branch": branch, "base": base, "environment": s.get("environment_id"),
        "url": f"https://claude.ai/code/{sid}",
        "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"adopted {sid} as {args.name} repo={repo.name} branch={branch or 'none yet'} "
          f"state={session_word(s)}" + ("" if args.brief_file else
          " (no brief: `review` needs one, pass --brief-file)"))


def cmd_envs(args):
    """One line per cloud environment: name, id, kind, state, and its role in the config."""
    cfg = load_config()
    envs = cloud_environments()
    if args.json:
        print(json.dumps(envs, indent=2))
        return
    if not envs:
        print("no cloud environments: create one on claude.ai/code (or with /remote-env)")
        return
    names = {}
    for n, i in cfg.get("environments", {}).items():
        names.setdefault(i, []).append(n)
    default = cfg.get("cloud_environment")
    default = cfg.get("environments", {}).get(default, default)
    for e in envs:
        notes = [f"config name: {', '.join(names[e['id']])}"] if e["id"] in names else []
        if default and default in (e["id"], e["name"]):
            notes.append("cloud_environment")
        print(f"{e['name']!r} id={e['id']} kind={e['kind']} state={e['state']}"
              + (f" ({'; '.join(notes)})" if notes else ""))
    known = {e["id"] for e in envs}
    for n, i in cfg.get("environments", {}).items():
        if i not in known:
            print(f"warning: the config name {n!r} points to {i}, which this login "
                  f"({claude_config_dir() or '~/.claude'}) does not have. It may belong to "
                  "another claude login (CLAUDE_CONFIG_DIR).")


def main():
    p = argparse.ArgumentParser(prog="dispatch.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    for where, fn in (("local", cmd_local), ("cloud", cmd_cloud)):
        s = sub.add_parser(where, help=f"start a {where} session")
        s.add_argument("--repo", required=True, help="the project directory")
        s.add_argument("--name", required=True, help="short unique task name")
        s.add_argument("--brief-file", required=True, help="file holding the task brief")
        s.add_argument("--model", help="model for this session (default: the config's "
                                       f"{where}_model, else the account's default)")
        if where == "local":
            s.add_argument("--no-worktree", action="store_true",
                           help="work in the repo itself, not in a new git worktree")
            s.add_argument("--needs-local", action="store_true",
                           help="mark the task as unable to move to the cloud")
        else:
            s.add_argument("--base", help="branch the cloud session starts from")
            s.add_argument("--allow-unpushed", action="store_true")
            s.add_argument("--environment", help="cloud environment: a name from the config's "
                           "`environments`, or an id (env_... or ccpool_...)")
        s.set_defaults(fn=fn)

    s = sub.add_parser("status", help="one line per task")
    s.add_argument("--repo", help="only the tasks of this project directory")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("wait", help="block until tasks report or stop, then print status")
    s.add_argument("names", nargs="*", help="task names (default: every task)")
    s.add_argument("--timeout", type=int, default=540, help="seconds to wait at most")
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("envs", help="list the account's cloud environments (claude.ai/code)")
    s.add_argument("--json", action="store_true", help="print them as JSON")
    s.set_defaults(fn=cmd_envs)
    s = sub.add_parser("sessions", help="list the account's cloud sessions (what teleport lists)")
    s.add_argument("--all", action="store_true", help="archived sessions too")
    s.add_argument("--limit", type=int, default=30, help="at most this many (default 30)")
    s.add_argument("--json", action="store_true", help="print them as JSON")
    s.set_defaults(fn=cmd_sessions)
    s = sub.add_parser("adopt", help="track a cloud session that dispatch did not start")
    s.add_argument("session", help="its id, session_... or cse_... (`sessions` lists them)")
    s.add_argument("--name", required=True, help="short unique task name")
    s.add_argument("--repo", required=True, help="a local checkout of the session's repository")
    s.add_argument("--brief-file", help="what the session was asked to do (a review needs it)")
    s.set_defaults(fn=cmd_adopt)
    for name, fn, text in (
        ("review", cmd_review, "start a new session that reviews a finished task's branch"),
        ("result", cmd_result, "print a task's report"),
        ("clean", cmd_clean, "remove a finished local task's session and worktree"),
        ("forget", cmd_forget, "drop a task from the list"),
    ):
        s = sub.add_parser(name, help=text)
        s.add_argument("name")
        if name == "review":
            s.add_argument("--model", help="model for the review session")
        s.set_defaults(fn=fn)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
