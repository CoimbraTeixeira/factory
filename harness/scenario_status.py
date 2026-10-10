#!/usr/bin/env python3
"""Run the hidden scenarios against every open pull request and post the result as a
`scenarios` commit status, which branch protection requires before merging.

The scenarios cannot run in GitHub Actions -- its logs are public -- so this runs here,
every few minutes from cron, for any PR head that has no `scenarios` status yet. It
never says which scenarios failed: the status reads "19/21 hidden scenarios satisfied",
because a PR author, human or agent, who learns the checks can satisfy them without
making the code right.

The PR's code runs in the agent image with no network. It can read the scenarios (it is
the code under test) but has nowhere to send them; only the runner's report comes back.

  ./harness/scenario_status.py              # every open PR on the monitor repos
  ./harness/scenario_status.py --dry-run    # run, print, post nothing
"""

import argparse
import fcntl
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

FACTORY_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = FACTORY_DIR / "state"
OWNER = "CoimbraTeixeira"
MONITOR_REPOS = ("congress-trades", "fare-monitor", "value-screener")
CONTEXT = "scenarios"
IMAGE = "factory-agent:latest"
SCENARIOS_DIR = Path.home() / "factory-scenarios"
RUN_TIMEOUT = 900


def gh_json(*args: str):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else None


def has_status(repo: str, sha: str) -> bool:
    statuses = gh_json("api", f"repos/{OWNER}/{repo}/commits/{sha}/statuses") or []
    return any(s.get("context") == CONTEXT for s in statuses)


def post_status(repo: str, sha: str, state: str, description: str) -> None:
    subprocess.run(["gh", "api", f"repos/{OWNER}/{repo}/statuses/{sha}", "-f",
                    f"state={state}", "-f", f"context={CONTEXT}", "-f",
                    f"description={description[:140]}"],
                   capture_output=True, text=True, check=True)


def extract_tarball(data: bytes, dest: Path) -> None:
    """GitHub tarballs wrap the tree in an `<owner>-<repo>-<sha>/` directory; strip it.
    Members are filtered by tarfile's 'data' policy, so no path escapes `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = []
        for member in archive.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            member.name = parts[1]
            members.append(member)
        archive.extractall(dest, members=members, filter="data")


def download(repo: str, ref: str, dest: Path) -> None:
    data = subprocess.run(["gh", "api", f"repos/{OWNER}/{repo}/tarball/{ref}"],
                          capture_output=True, check=True).stdout
    extract_tarball(data, dest)


def describe(report: dict) -> tuple[str, str]:
    """(state, description) for a runner report. Counts only -- never scenario IDs."""
    outcomes = report.get("outcomes", [])
    satisfied = sum(1 for o in outcomes if o.get("satisfied"))
    total = len(outcomes)
    if not total:
        return "error", "no scenarios ran"
    state = "success" if satisfied == total else "failure"
    return state, f"{satisfied}/{total} hidden scenarios satisfied"


def evaluate(repo: str, sha: str, workdir: Path) -> tuple[str, str]:
    """Assemble master of every monitor repo with this PR's tree in place of its repo,
    run the scenarios over it in a network-less container, and describe the result."""
    repos = workdir / "repos"
    for name in MONITOR_REPOS:
        download(name, sha if name == repo else "master", repos / name)
    out = workdir / "out"
    out.mkdir()
    cmd = ["docker", "run", "--rm", "--network", "none", "--user", "1000:1000",
           "-e", "HOME=/tmp/home", "-e", "FACTORY_SCENARIOS_DIR=/scenarios",
           "-v", f"{FACTORY_DIR}:/factory:ro", "-v", f"{SCENARIOS_DIR}:/scenarios:ro",
           "-v", f"{repos}:/repos:ro", "-v", f"{out}:/out",
           IMAGE, "python", "/factory/scenarios/runner.py", "--repos-dir", "/repos",
           "--report", "/out/report.json"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    report_path = out / "report.json"
    if not report_path.exists():
        return "error", "scenario run produced no report"
    return describe(json.loads(report_path.read_text()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "scenario_status.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        for repo in MONITOR_REPOS:
            prs = gh_json("pr", "list", "-R", f"{OWNER}/{repo}", "--state", "open",
                          "--json", "number,headRefOid") or []
            for pr in prs:
                sha = pr["headRefOid"]
                if has_status(repo, sha):
                    continue
                if not args.dry_run:
                    post_status(repo, sha, "pending", "running hidden scenarios")
                with tempfile.TemporaryDirectory(prefix="scenario-status-") as tmp:
                    try:
                        state, description = evaluate(repo, sha, Path(tmp))
                    except Exception as exc:
                        state, description = "error", f"harness error: {type(exc).__name__}"
                print(f"{repo}#{pr['number']} {sha[:7]}: {state} -- {description}")
                if not args.dry_run:
                    post_status(repo, sha, state, description)
    return 0


if __name__ == "__main__":
    sys.exit(main())
