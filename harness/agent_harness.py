#!/usr/bin/env python3
"""Turn GitHub issues labelled `agent` into pull requests written by a coding agent.

Hourly from cron, it takes the oldest open `agent` issue across the four repos, clones
that repo, and runs `omp -p --auto-approve` on it inside the factory-agent container.
If the agent commits a change, the harness pushes the branch and opens a PR that fixes
the issue. It never merges: CI and the hidden-scenario status (harness/scenario_status.py)
gate the PR, and you decide.

Containment, because --auto-approve gives the agent a full shell:
- The container sees only a fresh clone of one repo and an empty home. Not
  ~/factory-scenarios (the held-out tests), not ~/.config (the Discord webhook), not the
  live checkouts or their state, not ~/.omp (whose session history describes the
  scenarios).
- No credentials enter it. Model access goes through harness/gateway_relay.py; GitHub
  access stays here, outside: the agent commits, the harness pushes.
- A change touching the gates themselves -- CI workflow, secret-scan hook, lint
  config -- is refused rather than pushed: an agent must not be able to grade itself.

Limits: at most MAX_RUNS_PER_DAY runs a day, MAX_ATTEMPTS per issue, RUN_TIMEOUT a run.

  ./harness/agent_harness.py             # what cron runs
  ./harness/agent_harness.py --dry-run   # pick the issue and print the prompt; no run
"""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway_relay import Relay  # noqa: E402

FACTORY_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = FACTORY_DIR / "state" / "agent_harness.json"
OWNER = "CoimbraTeixeira"
REPOS = ("congress-trades", "fare-monitor", "value-screener", "factory")
LABEL = "agent"
IMAGE = "factory-agent:latest"
MODEL = "anthropic/claude-opus-5-5"
DOCKER_HOST_IP = "172.17.0.1"

MAX_RUNS_PER_DAY = 2
MAX_ATTEMPTS = 2
RUN_TIMEOUT = 30 * 60

TEST_COMMANDS = {
    "fare-monitor": "python3 -m unittest test_fare_monitor",
}
DEFAULT_TEST_COMMAND = "python3 -m unittest discover -s tests"

# Paths an agent change may not touch: the gates it is judged by.
PROTECTED_PREFIXES = (".github/", "hooks/", "ruff.toml")

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "factory-agent", "GIT_AUTHOR_EMAIL": "rogerio.teixeira@gmail.com",
    "GIT_COMMITTER_NAME": "factory-agent", "GIT_COMMITTER_EMAIL": "rogerio.teixeira@gmail.com",
}


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def gh_json(*args: str):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else None


def branch_for(issue_number: int) -> str:
    return f"agent/issue-{issue_number}"


# --- State ------------------------------------------------------------------------------


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    return {"attempts": state.get("attempts", {}), "runs": state.get("runs", [])}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def runs_today(state: dict, now: datetime) -> int:
    today = now.date().isoformat()
    return sum(1 for run in state["runs"] if run["started_at"][:10] == today)


def issue_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


# --- Picking work -----------------------------------------------------------------------


def candidate_issues(state: dict) -> list[dict]:
    """Open `agent` issues not yet out of attempts and without an open agent PR,
    oldest first."""
    found = []
    for repo in REPOS:
        issues = gh_json("issue", "list", "-R", f"{OWNER}/{repo}", "--label", LABEL,
                         "--state", "open", "--json", "number,title,body,url,createdAt") or []
        for issue in issues:
            if state["attempts"].get(issue_key(repo, issue["number"]), 0) >= MAX_ATTEMPTS:
                continue
            open_prs = gh_json("pr", "list", "-R", f"{OWNER}/{repo}", "--state", "open",
                               "--head", branch_for(issue["number"]), "--json", "number")
            if open_prs:
                continue
            found.append(dict(issue, repo=repo))
    return sorted(found, key=lambda issue: issue["createdAt"])


def build_prompt(issue: dict) -> str:
    repo, number = issue["repo"], issue["number"]
    tests = TEST_COMMANDS.get(repo, DEFAULT_TEST_COMMAND)
    return f"""You are working in /work, a git clone of github.com/{OWNER}/{repo}, on branch
{branch_for(number)}. Resolve this GitHub issue:

# #{number}: {issue['title']}

{issue.get('body') or '(no description)'}

How to work:
- Read the README and the code around the problem first. Follow the repo's existing
  conventions: comments that explain why, docstrings, the existing test files and style.
- Make the smallest change that fully resolves the issue. No unrelated refactors.
- Add or update tests that would fail without your change.
- Update the README when behaviour or usage changes.
- These gates must all pass before you commit; run them yourself:
    python3 hooks/pre-commit --all
    ruff check .
    {tests}
- Do not modify .github/, hooks/ or ruff.toml. A change there is rejected unread.
- Commit your work on the current branch with a message ending "(#{number})". Do not
  push: there are no GitHub credentials here, and the harness pushes for you.
- If the issue is unclear, already resolved, or cannot be done well, commit nothing and
  explain why.

Finish with a short pull-request description: what changed, why, and how you verified
it (commands run and their results)."""


# --- Running the agent ------------------------------------------------------------------


def write_agent_home(home: Path, relay: Relay) -> None:
    """An omp config whose only model provider is the relay."""
    agent_dir = home / ".omp" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yml").write_text(f"modelRoles:\n  default: {MODEL}\n")
    (agent_dir / "models.yml").write_text(
        "providers:\n  anthropic:\n    transport: pi-native\n"
        f"    baseUrl: {relay.base_url}\n    apiKey: {relay.token}\n")


def run_agent(clone: Path, home: Path, prompt: str) -> subprocess.CompletedProcess:
    omp = shutil.which("omp")
    if not omp:
        raise RuntimeError("omp not found on PATH")
    env_args = [arg for key, value in GIT_IDENTITY.items() for arg in ("-e", f"{key}={value}")]
    cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
           "-e", "HOME=/home/agent", *env_args,
           "-v", f"{clone}:/work", "-v", f"{home}:/home/agent",
           "-v", f"{omp}:/usr/local/bin/omp:ro",
           IMAGE, "timeout", str(RUN_TIMEOUT), "omp", "-p", "--auto-approve", prompt]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT + 120)


def new_commits(clone: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(clone), "rev-list", "origin/master..HEAD"],
                         capture_output=True, text=True, check=True).stdout
    return out.split()


def changed_files(clone: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(clone), "diff", "--name-only",
                          "origin/master...HEAD"], capture_output=True, text=True,
                         check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def protected_changes(files: list[str]) -> list[str]:
    return [f for f in files if f.startswith(PROTECTED_PREFIXES)]


def comment(repo: str, number: int, text: str) -> None:
    subprocess.run(["gh", "issue", "comment", str(number), "-R", f"{OWNER}/{repo}",
                    "--body", text], capture_output=True, text=True, check=True)


def summary_of(output: str, limit: int = 6000) -> str:
    text = output.strip()
    return text if len(text) <= limit else "…\n" + text[-limit:]


def work_on(issue: dict, state: dict) -> dict:
    repo, number = issue["repo"], issue["number"]
    key = issue_key(repo, number)
    attempt = state["attempts"].get(key, 0) + 1
    state["attempts"][key] = attempt
    run = {"started_at": datetime.now(timezone.utc).isoformat(), "issue": key,
           "attempt": attempt}
    state["runs"].append(run)
    save_state(state)  # a crash mid-run still counts against the limits

    with tempfile.TemporaryDirectory(prefix=f"agent-{repo}-{number}-") as tmp:
        clone, home = Path(tmp) / "work", Path(tmp) / "home"
        subprocess.run(["git", "clone", "-q", f"https://github.com/{OWNER}/{repo}.git",
                        str(clone)], check=True)
        subprocess.run(["git", "-C", str(clone), "switch", "-q", "-c", branch_for(number)],
                       check=True)
        with Relay(DOCKER_HOST_IP) as relay:
            write_agent_home(home, relay)
            log(f"{key}: agent run {attempt}/{MAX_ATTEMPTS} started")
            result = run_agent(clone, home, build_prompt(issue))
        output = summary_of(result.stdout or result.stderr)
        commits = new_commits(clone)
        run.update(exit=result.returncode, commits=len(commits))

        if not commits:
            run["outcome"] = "no-change"
            comment(repo, number, f"🤖 Agent attempt {attempt}/{MAX_ATTEMPTS} made no change "
                                  f"(exit {result.returncode}). Its notes:\n\n{output}")
            return run
        refused = protected_changes(changed_files(clone))
        if refused:
            run["outcome"] = "refused-protected"
            comment(repo, number, f"🤖 Agent attempt {attempt}/{MAX_ATTEMPTS} changed the "
                                  f"gates ({', '.join(refused)}), so it was not pushed.")
            return run
        subprocess.run(["git", "-C", str(clone), "push", "-q",
                        f"https://github.com/{OWNER}/{repo}.git",
                        f"HEAD:refs/heads/{branch_for(number)}"], check=True)
        body = (f"Fixes #{number}\n\nOpened by the factory agent harness "
                f"(attempt {attempt}/{MAX_ATTEMPTS}). Merging needs CI and the hidden "
                f"`scenarios` status; review before merging.\n\n---\n\n{output}")
        url = subprocess.run(["gh", "pr", "create", "-R", f"{OWNER}/{repo}", "--base",
                              "master", "--head", branch_for(number), "--title",
                              issue["title"], "--body", body],
                             capture_output=True, text=True, check=True).stdout.strip()
        run.update(outcome="pr-opened", pr=url)
        return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="pick the issue and print the prompt; run nothing")
    args = parser.parse_args()

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH.with_suffix(".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another harness run is active; skipping.")
            return 0
        state = load_state()
        now = datetime.now(timezone.utc)
        if runs_today(state, now) >= MAX_RUNS_PER_DAY:
            log(f"Daily limit reached ({MAX_RUNS_PER_DAY} runs); nothing started.")
            return 0
        candidates = candidate_issues(state)
        if not candidates:
            log("No open `agent` issues to work on.")
            return 0
        issue = candidates[0]
        if args.dry_run:
            log(f"Would work on {issue['repo']}#{issue['number']}: {issue['title']}")
            print(build_prompt(issue))
            return 0
        try:
            run = work_on(issue, state)
        finally:
            save_state(state)
        log(f"{run['issue']}: {run.get('outcome', 'failed')} {run.get('pr', '')}".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
