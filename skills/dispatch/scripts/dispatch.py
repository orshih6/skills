#!/usr/bin/env python3
"""dispatch: start Claude Code sessions (local background or cloud) and track them.

Standard library only. State lives in ~/.claude/dispatch/ (never in a repo):

  config.json        optional local settings (see SKILL.md)
  tasks.jsonl        one line per dispatched task
  results/<name>.md  what each local session reports when it stops
  briefs/<name>.md   the exact brief each session was given

A review is a local task named <name>-review: a new session that did not write the
change reads the finished task's branch and reports a verdict.

Commands: local, cloud, review, fix, wait, status, envs, sessions, adopt, pull, result, clean, forget. Run with -h for flags.
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
import tarfile
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
progress to `{branch}` (do not push it), and stop.
{notify}"""

UPLOAD_PUSH = """\
- This folder was uploaded from the owner's computer. Do not push anything anywhere
  and do not open a pull request, even if the project's own instructions say to:
  the owner copies your work back from this conversation."""

UPLOAD_REPORT = """\
## Reporting (required, last step)

Your work cannot leave this machine through git: the owner copies it back from this
conversation. So, as the last step:

1. Send every file you created or changed with the SendUserFile tool, under the same
   paths. When there are more than 20, put them in one `dispatch-{name}.tar.gz` (with
   their paths from the folder's root) and send that instead.
2. End your last message with these lines:

    DISPATCH: {name}
    STATUS: done | failed | blocked
    TESTS: <command you ran and whether it passed, or not run and why>
    SUMMARY: <2 to 5 lines: what changed, what is left, anything the owner must decide>
"""

CLOUD_REVIEW_SEND = """
This machine is a fresh copy, uploaded from the owner's computer: nothing was installed
on it before you. If the checks cannot run because something is missing that the
project's own files never mention (a dependency, a setup step, a file that was never
committed), that is a finding too. Do not push anything anywhere.

After writing the file, send it with the SendUserFile tool: the owner reads your result
from that file only. Then end your last message with the same text.
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

{place} `git checkout --detach {ref}` puts the changed code
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
    for t in tasks:  # these are only written when they hold something
        for k in ("model", "environment", "upload"):
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


def session_busy(f):
    """True while a cloud session works, or has not yet ended its first turn."""
    return f["worker"] == "running" or (not f["archived"] and not f["category"]
                                        and f["worker"] != "requires_action")


def session_facts(s):
    """The fields of a cloud session that dispatch uses, with defaults."""
    meta = s.get("external_metadata") or {}
    summary = s.get("post_turn_summary") or meta.get("post_turn_summary") or {}
    branches = [b for b in (meta.get("current_branches") or {}).values() if b]
    return {
        "archived": s.get("status") == "archived",
        "worker": s.get("worker_status") or "",
        # the session's own end-of-turn summary says how a turn ended; before the first
        # turn ends there is none, and status_bucket can already read "completed" then
        "category": summary.get("status_category")
        or (s.get("status_bucket") if s.get("status_bucket") in ("failed", "blocked") else ""),
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
    claude.ai/code (any case). Every id is checked against the environments of the
    login `claude --cloud` will use: a config id from another login (another
    CLAUDE_CONFIG_DIR) is passed over for the claude.ai name, and an id this login does
    not have stops the start, because the CLI would quietly run somewhere else. Nothing
    chosen: the account's default, whichever the CLI picks.
    """
    want = getattr(args, "environment", None) or cfg.get("cloud_environment")
    if not want:
        return None, None, []
    try:
        live = [e for e in cloud_environments() if e["state"] != "archived"]
    except SystemExit:  # claude.ai not reachable: trust what was given
        live = None
    ids = {e["id"]: e["name"] for e in live or []}
    mapped = {k.casefold(): v for k, v in cfg.get("environments", {}).items()}
    env_id = None
    for cand in (mapped.get(want.casefold()), want if want.startswith(("env_", "ccpool_")) else None):
        if cand and (live is None or cand in ids):
            env_id = cand
            break
    if not env_id and live is not None:
        hits = [e for e in live if e["name"].casefold() == want.casefold()]
        if len(hits) == 1:
            env_id = hits[0]["id"]
        else:
            names = sorted({e["name"] for e in live})
            die(f"{'several environments are' if hits else 'this claude login has no environment'} "
                f"named {want!r}" + (f" (the config maps it to {mapped[want.casefold()]}, which belongs "
                                     "to another login)" if want.casefold() in mapped else "")
                + f". This login has: {', '.join(repr(n) for n in names) or 'none'}. "
                "`envs` lists them; pass --environment, or fix cloud_environment.")
    if not env_id:
        env_id = mapped.get(want.casefold(), want)
    label = f"{ids[env_id]!r}" if env_id in ids else want
    if env_id.startswith("ccpool_"):  # a self-hosted environment has its own flag
        return label, env_id, ["--environment", env_id]
    # a hosted environment is chosen through the setting the CLI reads its default from
    return label, env_id, ["--settings", json.dumps({"remote": {"defaultEnvironmentId": env_id}})]


def session_title(name, cfg=None):
    """The name a session shows in `claude agents`, claude.ai/code and teleport's list:
    the config's session_prefix (default `dispatch-`) and the task name."""
    prefix = (cfg if cfg is not None else load_config()).get("session_prefix", "dispatch-")
    return name if name.startswith(prefix) else prefix + name


def start_bg(repo, name, prompt, worktree, model=None):
    """Start a local background session and return its short id."""
    cmd = ["claude", "--bg", "-n", session_title(name)]
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


def spawn_in_pty(cmd, cwd, timeout=90, env=None):
    """Run cmd on a pseudo-terminal (some claude flags refuse to run without one)."""
    master, slave = pty.openpty()
    p = subprocess.Popen(cmd, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                         start_new_session=True, env={**os.environ, **(env or {})})
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


def start_cloud(folder, name, prompt, model, env_args, cfg):
    """Upload `folder` as it is (CCR_FORCE_BUNDLE: never through GitHub) and start a cloud
    session on it. Gives (session id, link)."""
    out = spawn_in_pty(["claude", "--cloud", prompt, "-n", session_title(name, cfg)]
                       + (["--model", model] if model else []) + env_args, cwd=folder,
                       timeout=300, env={"CCR_FORCE_BUNDLE": "1"})
    if "trust this folder" in out:
        die(f"{folder} is not a trusted folder. The owner must run `claude` there once "
            "and accept the trust prompt. Do not accept it for them.", 3)
    m = re.search(r"(session_[A-Za-z0-9]+)", out)
    if "Created cloud session" not in out or not m:
        die("could not create the cloud session:\n" + out.strip()[-600:])
    url = re.search(r"https://claude\.ai/code/\S+", out)
    return m.group(1), (url.group(0).split("?")[0] if url else f"https://claude.ai/code/{m.group(1)}")


def cmd_cloud(args):
    cfg, tasks = load_config(), load_tasks()
    args.where = "cloud"
    repo, brief = check_common(args, tasks, cfg)
    # Every cloud task is uploaded: claude --cloud sends this folder itself (CCR_FORCE_BUNDLE
    # makes it do so for a GitHub repo too), and `pull` brings the work back. No GitHub,
    # no branch, no pull request.
    if any(repo.name == n or str(repo).endswith(n) for n in cfg.get("local_only", [])):
        die(f"{repo.name} is listed as local_only in {CONFIG}. Run it locally.", 4)
    if not git(repo, "rev-parse", "--git-dir"):
        die(f"{repo} is not a git repository; claude --cloud uploads a folder's git history. "
            "Run `git init` and commit the files first.", 4)
    if not git(repo, "rev-parse", "--verify", "--quiet", "HEAD"):
        die(f"{repo} has no commit yet; commit the files the task needs first.", 4)
    full = "\n\n".join([
        brief,
        RULES.format(branch=f"dispatch/{args.name}", extra=cfg.get("extra_rules", ""), push=UPLOAD_PUSH),
        UPLOAD_REPORT.format(name=args.name),
    ])
    BRIEFS.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{args.name}.md").write_text(full + "\n")

    model = pick_model(args, cfg, "cloud")
    env_name, env_id, env_args = pick_environment(args, cfg)
    sid, url = start_cloud(repo, args.name, full, model, env_args, cfg)
    task = {
        "name": args.name, "where": "cloud", "model": model, "environment": env_id,
        "id": sid, "repo": str(repo), "upload": True, "branch": None,
        "base": git(repo, "rev-parse", "HEAD") or None, "url": url,
        "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started cloud {args.name} id={task['id']} repo={repo.name} uploaded "
          "(`pull` brings the work back)"
          + (f" model={model}" if model else "")
          + f" environment={env_name or 'account default'}")
    print(f"view: {task['url']}")


def resolves(repo, ref):
    return bool(ref and git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}"))


def import_cloud(t):
    """Put a finished uploaded cloud task's work on a local branch, so `review` and `fix`
    treat it like a local task. Changes t; the caller saves it.

    The branch `cloud-<name>` starts at the commit the folder was uploaded from, in a
    worktree at `.claude/worktrees/<name>`, and gets one commit: every file the session
    sent (a `dispatch-<name>.tar.gz` is unpacked) or wrote, under its path in the folder.
    Files the session deleted, and uncommitted changes that were uploaded, are not told
    apart: the diff shows what the session's files hold.
    """
    repo, name = Path(t["repo"]), t["name"]
    base = t.get("base")
    if not base or not resolves(repo, base):
        die(f"the commit {name} was uploaded from ({base or 'unknown'}) is not in {repo}; "
            f"`pull {name}` and compare by hand")
    s = CloudSessions().get(t["id"])
    if not isinstance(s, dict):
        die(f"could not read {name} from claude.ai")
    if session_busy(session_facts(s)):
        die(f"{name} is still running; `wait {name}` first")
    token, org = claude_login()
    sent, written, skipped = session_files(session_events(t["id"], token, org))
    report = re.compile(r"/dispatch-report-[^/]*\.md$")

    def rel(path):  # /home/user/<folder>/<path> -> <path>
        parts = path.split("/")
        return "/".join(parts[4:]) if path.startswith("/home/user/") and len(parts) > 4 else None

    wt = repo / ".claude" / "worktrees" / name
    branch = f"cloud-{name}"
    if wt.exists() or resolves(repo, branch):
        die(f"{wt} or the branch {branch} already exists; remove them or `forget` and re-adopt")
    hide_worktrees(repo)
    r = run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", branch, str(wt), base])
    if r.returncode != 0:
        die(f"could not make the worktree: {r.stderr.strip()}")
    root = wt.resolve()
    placed = 0
    for path, text in written.items():
        rp = rel(path)
        if rp and path not in sent and not report.search(path):
            target = (wt / rp).resolve()
            if target.is_relative_to(root) and ".git" not in Path(rp).parts:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
                placed += 1
    for path, f in sent.items():
        rp = rel(path)
        if not rp or report.search(path):
            continue
        target = (wt / rp).resolve()
        if not target.is_relative_to(root) or ".git" in Path(rp).parts:
            continue
        if not download(f["uuid"], token, org, target, f.get("size")):
            die(f"could not download {path}; the worktree {wt} is left as it is")
        if rp == f"dispatch-{name}.tar.gz":
            with tarfile.open(target) as tar:
                members = tar.getmembers()
                bad = [m.name for m in members if not (wt / m.name).resolve().is_relative_to(root)
                       or m.issym() or m.islnk() or ".git" in Path(m.name).parts]
                if bad:
                    die(f"{path} holds paths that leave the folder or are links: {bad[:5]}")
                tar.extractall(wt, members=[m for m in members if m.isfile() or m.isdir()])
            target.unlink()
        placed += 1
    if not placed:
        die(f"{name} sent and wrote no files; there is nothing to review. Its worktree {wt} is empty of changes.")
    git(wt, "add", "-A")
    env = None
    if not git(wt, "config", "user.email"):
        env = {**os.environ, "GIT_AUTHOR_NAME": "dispatch", "GIT_AUTHOR_EMAIL": "dispatch@localhost",
               "GIT_COMMITTER_NAME": "dispatch", "GIT_COMMITTER_EMAIL": "dispatch@localhost"}
    r = subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m",
                        f"dispatch: work of cloud task {name}\n\nPulled from {t.get('url')}"],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        die(f"could not commit the pulled files: {(r.stdout + r.stderr).strip()[-400:]}")
    t.update(branch=branch, worktree=True, imported=True)
    print(f"imported {name}: {placed} file{'s' if placed != 1 else ''} on the branch {branch} "
          f"in {wt}" + (f" ({len(skipped)} edits could not be rebuilt)" if skipped else ""))


def review_range(t, repo):
    """The two commits a review compares: where the task's branch left its base, and its tip."""
    if not t.get("branch"):
        die(f"{t['name']} has no branch "
            + ("(claude.ai names none for this session yet)" if t["where"] == "cloud"
               else "(its folder is not a git repository)") + "; there is nothing to diff")
    cloud = t["where"] == "cloud" and not t.get("upload")  # a GitHub session's branch
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
    if t.get("upload") and not t.get("branch"):
        import_cloud(t)
        save_tasks(tasks)
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
    RESULTS.mkdir(parents=True, exist_ok=True)
    result.unlink(missing_ok=True)
    where, why = review_where(args, cfg, t, repo, ref)
    if where == "cloud":
        tip = git(repo, "rev-parse", ref)
        report = f"dispatch-report-{name}.md"
        full = REVIEW.format(
            brief=brief, base=base, ref=tip[:12], result=report,
            place="You are on a fresh machine with an uploaded copy of the repository.",
            extra=extra + "\n" if extra else "", notify="") + CLOUD_REVIEW_SEND
        full = full.replace(f"`{report}` in exactly", f"`{report}`, at the folder's root, in exactly")
        (BRIEFS / f"{name}.md").write_text(full + "\n")
        model = pick_model(args, cfg, "cloud")
        env_name, env_id, env_args = pick_environment(argparse.Namespace(environment=None), cfg)
        sid, url = start_cloud(why, name, full, model, env_args, cfg)
        task = {
            "name": name, "where": "cloud", "kind": "review", "of": args.name, "model": model,
            "environment": env_id, "id": sid, "repo": str(repo), "upload": True, "branch": None,
            "report": report, "url": url, "started": int(time.time()),
        }
        save_tasks(tasks + [task])
        print(f"started review {name} in the cloud id={sid} repo={repo.name} diff={base}..{tip[:12]}"
              f" environment={env_name or 'account default'}")
        print(f"view: {url}")
        return
    check_limit(tasks, cfg)
    full = REVIEW.format(brief=brief, base=base, ref=ref, result=result,
                         place="You are in your own git worktree.",
                         extra=extra + "\n" if extra else "", notify=notify_text(cfg, name))
    (BRIEFS / f"{name}.md").write_text(full + "\n")
    model = pick_model(args, cfg, "local")
    task = {
        "name": name, "where": "local", "kind": "review", "of": args.name, "model": model,
        "id": start_bg(repo, name, full, True, model), "repo": str(repo), "branch": None,
        "worktree": True, "needs_local": True, "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started review {name} on this machine id={task['id']} repo={repo.name} diff={base}..{ref}"
          + (f" ({why})" if why else ""))


def review_where(args, cfg, t, repo, ref):
    """Where a review runs: ("cloud", the folder to upload) or ("local", why not the cloud).

    The cloud by default: a fresh machine catches what only works on this one. Not when
    the task needs this machine, its repository is local_only, the owner said --local or
    review_in_cloud is false, or there is no folder whose checkout is the change's tip.
    """
    if getattr(args, "local", False):
        return "local", ""
    if cfg.get("review_in_cloud", True) is False:
        return "local", "review_in_cloud is false in the config"
    if t.get("needs_local"):
        return "local", "the task needs this machine"
    if any(repo.name == n or str(repo).endswith(n) for n in cfg.get("local_only", [])):
        return "local", f"{repo.name} is local_only"
    tip = git(repo, "rev-parse", ref)
    wt = repo / ".claude" / "worktrees" / t["name"]
    for folder in (wt, repo):
        if folder.is_dir() and git(folder, "rev-parse", "HEAD") == tip:
            try:
                claude_login(soft=True)
            except LoginMissing as e:
                return "local", f"no claude.ai login for the cloud: {e}"
            return "cloud", folder
    return "local", "no folder here has the change checked out to upload"


FIX = """\
## A review sent this work back

This is round {round} of the task above. The work so far is on the branch `{branch}`
and is already checked out here{where}. A review by another session, which ran the
checks itself, found these problems:

{findings}

Fix each of them on that branch. Keep what already works, and do not redo the task
from the start. For each problem, add or change a test that fails without your fix
when that is possible. Then run the checks the task asks for, and the ones the review
ran, and report.
"""

CONFIRMED_MAJOR = re.compile(r"^\s*\d+\.\s+MAJOR\s+CONFIRMED\b.*$", re.M)


def latest_round(tasks, name):
    """The newest work on a task: (the task or its last fix round, fix rounds so far)."""
    fixes = [t for t in tasks if t.get("kind") == "fix" and t.get("of") == name]
    if not fixes:
        return next(t for t in tasks if t["name"] == name), 0
    last = max(fixes, key=lambda t: t["round"])
    return last, last["round"]


def cmd_fix(args):
    """Send a task back to a new session with its review's MAJOR CONFIRMED findings."""
    cfg, tasks = load_config(), load_tasks()
    t = next((t for t in tasks if t["name"] == args.name), None)
    if not t:
        die(f"no task named {args.name}")
    if t.get("kind") in ("review", "fix"):
        die(f"{args.name} is a {t['kind']}; give the name of the task it belongs to "
            f"({t.get('of')})")
    if t["where"] == "cloud" and not t.get("imported"):
        die(f"{args.name} is a cloud task that has not been reviewed here; `review {args.name}` "
            "first (it brings the work to a local branch)")
    last, done = latest_round(tasks, args.name)
    rnd = done + 1  # this fix round's number
    limit = int(cfg.get("max_fix_rounds", 2))
    if done >= limit:
        die(f"{args.name} was already sent back {done} times (max_fix_rounds {limit}); "
            "the owner decides what happens now", 7)
    review = f"{last['name']}-review"
    rt = next((x for x in tasks if x["name"] == review), None)
    if rt:
        fetch_report(rt)
    rstat, _ = read_result(review)
    if not rstat:
        die(f"{last['name']} has no finished review yet (`review {last['name']}`, then "
            f"`wait {review}`)")
    text = (RESULTS / f"{review}.md").read_text()
    findings = CONFIRMED_MAJOR.findall(text)
    if not findings:
        die(f"the review {review} found nothing MAJOR and CONFIRMED; there is nothing to "
            "send back. MINOR or PLAUSIBLE findings are the owner's call.", 7)
    bf = BRIEFS / f"{args.name}.md"
    brief = bf.read_text().split(RULES_MARK)[0].strip() if bf.exists() else ""
    if not brief:
        die(f"the brief of {args.name} is missing ({bf})")
    name = f"{args.name}-fix{rnd}"
    if any(x["name"] == name for x in tasks):
        die(f"a task named {name} already exists; `forget` it to start the round again")
    found = "\n".join("    " + f.strip() for f in findings)
    repo = Path(t["repo"])

    wt = repo / ".claude" / "worktrees" / args.name if t.get("worktree") else repo
    if not wt.is_dir():
        die(f"{wt} is gone (the task was cleaned?); there is nothing to continue")
    live = sessions()
    if any(live.get(x["id"], {}).get("state") == "working" and not (RESULTS / f"{x['name']}.md").exists()
           for x in tasks if x["where"] == "local" and x["name"] in (args.name, last["name"])):
        die(f"a session of {args.name} is still working in {wt}; wait for it first")
    branch = t["branch"]
    if git(wt, "branch", "--show-current") != branch:
        die(f"{wt} is not on {branch}; something else moved it. Ask the owner.")
    dirty = [l for l in git(wt, "status", "--porcelain").splitlines()
             if not l[3:].endswith(JUNK + tuple(cfg.get("leftovers", [])))]
    if dirty:
        die(f"{wt} holds uncommitted changes; a new round would mix with them:\n  "
            + "\n  ".join(l[3:] for l in dirty))
    check_limit(tasks, cfg)
    result = RESULTS / f"{name}.md"
    full = "\n\n".join([
        brief,
        FIX.format(round=rnd + 1, branch=branch, where=f" in {wt}", findings=found),
        RULES.format(branch=branch, extra=cfg.get("extra_rules", ""), push=LOCAL_PUSH.format(branch=branch)),
        LOCAL_REPORT.format(result=result, branch=branch, notify=notify_text(cfg, name)),
    ])
    for d in (RESULTS, BRIEFS):
        d.mkdir(parents=True, exist_ok=True)
    (BRIEFS / f"{name}.md").write_text(full + "\n")
    result.unlink(missing_ok=True)
    model = pick_model(args, cfg, "local")
    task = {
        "name": name, "where": "local", "kind": "fix", "of": args.name, "round": rnd,
        "model": model, "id": start_bg(wt, name, full, False, model), "repo": str(repo),
        "cwd": str(wt), "branch": branch, "base": t.get("base"), "worktree": False,
        "needs_local": bool(t.get("needs_local")), "started": int(time.time()),
    }
    save_tasks(tasks + [task])
    print(f"started fix {name} id={task['id']} repo={repo.name} branch={branch} "
          f"round {rnd + 1} of {limit + 1}, {len(findings)} finding{'s' if len(findings) != 1 else ''}")


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
                busy = isinstance(s, dict) and session_busy(session_facts(s))
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


def fetch_report(t, cs=None):
    """Copy a finished cloud review's report into results/<name>.md, once.

    The review sends it with SendUserFile; if it did not, its last message that holds a
    VERDICT line is used. Gives True when results/<name>.md exists afterwards.
    """
    f = RESULTS / f"{t['name']}.md"
    if f.exists():
        return True
    if t.get("where") != "cloud" or t.get("kind") != "review":
        return False
    s = (cs or CloudSessions()).get(t["id"])
    if not isinstance(s, dict) or session_busy(session_facts(s)):
        return False
    try:
        token, org = claude_login(soft=True)
    except LoginMissing:
        return False
    events = session_events(t["id"], token, org)
    sent, written, _ = session_files(events)
    RESULTS.mkdir(parents=True, exist_ok=True)
    for path, info in sent.items():
        if path.endswith("/" + t.get("report", "")):
            if download(info["uuid"], token, org, f, info.get("size")):
                return True
    for path, text in written.items():
        if path.endswith("/" + t.get("report", "")) and "VERDICT:" in text:
            f.write_text(text)
            return True
    for p in reversed(events):
        content = (p.get("message") or {}).get("content") if p.get("type") == "assistant" else None
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text") \
            if isinstance(content, list) else ""
        if "VERDICT:" in text and "STATUS:" in text:
            f.write_text(text[text.index("STATUS:"):].strip() + "\n")
            return True
    return False


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
    if t.get("upload"):  # no GitHub: only claude.ai knows, and `pull` brings the work
        if s == GONE:
            return "gone", "claude.ai no longer has this session; its work cannot be pulled"
        if not f:
            return "running-or-no-output", url
        line = f["line"] or f["recent"]
        if f["worker"] == "running":
            return "running", line
        if session_busy(f):
            return "starting", "the session has not finished its first turn"
        if f["worker"] == "requires_action":
            return "waiting-for-you", f"{f['needs'] or line or 'it waits for an answer'}; open {url}"
        if f["category"] == "failed":
            return "failed", f"{line}; open {url}"
        if f["category"] in ("blocked", "waiting"):
            return "blocked", f"{f['needs'] or line}; open {url}"
        return "done", f"{line}; `pull {t['name']}` brings its files"
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
        if t["where"] == "cloud" and t.get("kind") == "review" and fetch_report(t, cs):
            state, note = read_result(t["name"])
        elif t["where"] == "cloud":
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
        where = {"review": "review", "fix": "fix"}.get(t.get("kind"), t["where"])
        print(f"{t['name']:<28} {where:<6} {state:<20} {age:>4}m  {Path(t['repo']).name}  {note if 'https://' in note or len(note) <= 90 else note[:89] + '…'}")
    if cs.problem and any(t["where"] == "cloud" for t in tasks):
        print(f"note: claude.ai could not be read ({cs.problem}); cloud states come from GitHub only")


def cmd_result(args):
    t = next((t for t in load_tasks() if t["name"] == args.name), None)
    if not t:
        die(f"no task named {args.name}")
    if t["where"] == "cloud" and t.get("kind") == "review" and fetch_report(t):
        print((RESULTS / f"{args.name}.md").read_text())
        return
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
    if t["where"] != "local" and not t.get("imported"):
        die(f"{args.name} is a cloud task; there is nothing on this machine to remove. Use `forget`.")
    if t.get("imported"):  # its worktree holds pulled files; the session lives on claude.ai
        wt = Path(t["repo"]) / ".claude" / "worktrees" / args.name
        if wt.is_dir():
            junk = JUNK + tuple(load_config().get("leftovers", []))
            real = [l[3:] for l in git(wt, "status", "--porcelain").splitlines() if not l[3:].endswith(junk)]
            if real:
                die(f"{wt} holds changes that are not committed:\n  " + "\n  ".join(real)
                    + "\nAsk the owner what to do with them. Nothing was removed.", 6)
            r = run(["git", "-C", t["repo"], "worktree", "remove", "--force", str(wt)])
            if r.returncode != 0:
                die(f"could not remove {wt}: {r.stderr.strip()}", 6)
        save_tasks([x for x in tasks if x["name"] != args.name])
        print(f"removed {args.name}: worktree gone; its pulled work is on the branch {t['branch']}")
        return
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
        prefix = load_config().get("session_prefix", "dispatch-")
        found += [r for r in rows if (args.all or r.get("status") != "archived")
                  and (not args.dispatch or (r.get("title") or "").startswith(prefix))]
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


def session_events(sid, token, org):
    """Every event of a cloud session, oldest first (the history claude.ai shows)."""
    cse = "cse_" + sid[len("session_"):] if sid.startswith("session_") else sid
    found, cursor = [], None
    for _ in range(400):
        q = "?limit=500&sort_order=desc" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        page = api_get(f"/v1/code/sessions/{urllib.parse.quote(cse)}/events" + q, token, org)
        rows = page.get("data") or []
        found += rows
        cursor = page.get("next_cursor")
        if not rows or not cursor:
            break
    return [e.get("payload") or {} for e in reversed(found)]


def session_files(events):
    """What a session's history holds of its files, in the order it made them.

    Gives (sent, written, skipped): `sent` maps a path to the file_uuid of a file the
    session sent with SendUserFile (the real bytes, downloadable); `written` maps a
    path to its text as the session's Write and Edit calls left it (an Edit starts from
    the file as it was before, which its result records); `skipped` lists paths and why.
    Files that commands made (builds, renders, `cp`) are in neither: the history only
    shows that the command ran.
    """
    uses, results = {}, {}
    for p in events:
        content = (p.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_use":
                uses[b.get("id")] = b
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = (b.get("is_error") is True, p.get("tool_use_result"))
    sent, written, skipped = {}, {}, []
    for uid, use in uses.items():
        err, tur = results.get(uid, (True, None))
        if err:
            continue  # refused or failed: it changed nothing
        name, inp = use.get("name"), use.get("input") or {}
        tur = tur if isinstance(tur, dict) else {}
        if name == "SendUserFile":
            for a in tur.get("attachments") or []:
                if a.get("file_uuid") and a.get("path"):
                    sent[a["path"]] = {"uuid": a["file_uuid"], "size": a.get("size")}
        elif name == "Write" and isinstance(inp.get("content"), str):
            written[inp.get("file_path")] = inp["content"]
        elif name == "Edit" and inp.get("file_path"):
            path = inp["file_path"]
            base = written.get(path, tur.get("originalFile"))
            old, new = inp.get("old_string"), inp.get("new_string")
            if not isinstance(base, str) or not isinstance(old, str) or old not in base:
                skipped.append((path, "an edit whose starting text is not in the history"))
                written.pop(path, None)
                continue
            written[path] = base.replace(old, new) if inp.get("replace_all") else base.replace(old, new, 1)
    return sent, written, skipped


def local_path(out, path):
    """Where a path of the cloud machine goes under `out`, or None if it would leave it."""
    rel = path[len("/home/user/"):] if path.startswith("/home/user/") else path.lstrip("/")
    target = (out / rel).resolve()
    return target if target.is_relative_to(out.resolve()) and rel else None


def download(uuid, token, org, target, size=None):
    """Save a file the session sent (claude.ai's files route) to target; True when whole."""
    url = os.environ.get("DISPATCH_API_URL", "https://api.anthropic.com")
    req = urllib.request.Request(f"{url}/api/oauth/files/{urllib.parse.quote(uuid)}/content", headers={
        "Authorization": f"Bearer {token}", "x-organization-uuid": org,
        "anthropic-version": "2023-06-01"})
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except (urllib.error.URLError, TimeoutError, OSError):
        part.unlink(missing_ok=True)
        return False
    if size is not None and part.stat().st_size != size:
        part.unlink(missing_ok=True)
        return False
    part.replace(target)
    return True


def cmd_pull(args):
    """Bring a cloud session's files to this machine from its history, without GitHub."""
    t = next((t for t in load_tasks() if t["name"] == args.target), None)
    sid = session_id(t["id"] if t else args.target)
    if not t and not sid.startswith("session_"):
        die(f"no task named {args.target}, and it is not a session id; `sessions` lists them")
    if t and t["where"] != "cloud":
        die(f"{args.target} is a local task; its work is already on this machine")
    label = t["name"] if t else sid
    out = Path(args.out or Path.home() / "claude-cloud" / label).expanduser()
    if out.exists() and any(out.iterdir()) and not args.into_existing:
        die(f"{out} is not empty. Choose another --out, or pass --into-existing to add to it "
            "(files of the same name are replaced).")
    token, org = claude_login()
    if CloudSessions().get(sid) == GONE:
        die(f"claude.ai has no session {sid}")
    print(f"reading the history of {sid} ...", flush=True)
    sent, written, skipped = session_files(session_events(sid, token, org))
    print(f"{len(sent)} sent file{'s' if len(sent) != 1 else ''} to download, "
          f"{len(written)} written file{'s' if len(written) != 1 else ''} to rebuild", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    lines, failed = [], 0
    for path, f in sent.items():
        target = local_path(out, path)
        ok = target is not None and download(f["uuid"], token, org, target, f.get("size"))
        failed += not ok
        lines.append(f"{'sent' if ok else 'FAILED'}  {path}" + (f"  ({f.get('size')} bytes)" if f.get("size") else ""))
        print("  " + lines[-1], flush=True)
    for path, text in written.items():
        if path in sent:
            continue  # the real bytes win over a rebuilt text
        target = local_path(out, path)
        if target is None:
            skipped.append((path, "it would land outside the folder"))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        lines.append(f"rebuilt  {path}")
    lines += [f"skipped  {p}: {why}" for p, why in skipped]
    note = ("Pulled from the history of " + sid + " (https://claude.ai/code/" + sid + ").\n"
            "sent: the file the session sent, byte for byte. rebuilt: the text its Write and "
            "Edit calls left. Files that commands made (builds, renders, copies) are not in the "
            "history; ask the session to send them with SendUserFile, then pull again.\n\n")
    (out / "PULLED.md").write_text(note + "\n".join(lines) + "\n")
    print(f"pulled {len(lines) - len(skipped) - failed} file{'s' if len(lines) - len(skipped) - failed != 1 else ''} "
          f"of {label} into {out}" + (f"; {failed} download{'s' if failed != 1 else ''} failed" if failed else "")
          + (f"; {len(skipped)} skipped" if skipped else "") + " (list in PULLED.md)")
    if failed:
        sys.exit(6)


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
    s = sub.add_parser("fix", help="send a task back with its review's MAJOR CONFIRMED findings")
    s.add_argument("name", help="the task (not its review or an earlier fix)")
    s.add_argument("--model", help="model for the new round")
    s.set_defaults(fn=cmd_fix)
    s = sub.add_parser("pull", help="bring a cloud session's files here from its history (no GitHub needed)")
    s.add_argument("target", help="a cloud task's name, or a session id")
    s.add_argument("--out", help="folder to put them in (default ~/claude-cloud/<name>)")
    s.add_argument("--into-existing", action="store_true", help="add to a folder that is not empty")
    s.set_defaults(fn=cmd_pull)
    s = sub.add_parser("sessions", help="list the account's cloud sessions (what teleport lists)")
    s.add_argument("--all", action="store_true", help="archived sessions too")
    s.add_argument("--dispatch", action="store_true",
                   help="only sessions whose name has the session_prefix (dispatch-)")
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
            s.add_argument("--local", action="store_true",
                           help="review on this machine, not in the cloud")
        s.set_defaults(fn=fn)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
