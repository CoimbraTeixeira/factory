#!/usr/bin/env python3
"""Run a monitor for real against the fake services, inside a throwaway sandbox.

A sandbox is a temp directory holding everything a monitor writes or reads besides its
code -- data dirs, state, a webhook config naming the fake webhook, the fare sources
pointed at the fake server, value-screener's recorded Yahoo cache -- plus the fake server
on a free loopback port. `Sandbox.env(repo)` is the environment that wires one repo's
seams to it; `Sandbox.run()` runs a script with that environment and returns what the
fake webhook received.

Defence in depth, because the scripts it runs are the same ones cron runs hourly:

- HOME is the sandbox, so any path a seam missed resolves inside it. In particular the
  real ~/.config/fare-monitor/config.json is unreachable: a monitor that ignored
  MONITOR_CONFIG finds no webhook and exits instead of posting to the real channel.
- The seam variables are rebuilt from scratch, never inherited from the caller's shell.
- A checkout whose code does not read every seam is refused before anything runs: run
  from a pre-seam checkout, a monitor would write its state into that working tree.

  ./fakes/sandbox.py smoke --repos-dir ~/Documents/repos      # each monitor once
  ./fakes/sandbox.py run congress-trades congress_monitor.py --verbose
"""

import argparse
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from fake_services import DEFAULT_RECORDINGS, FakeServices, read_posts

DEFAULT_REPOS_DIR = Path.home() / "Documents" / "repos"

# Every variable each repo must read for a sandboxed run to be safe. Shared names: the
# same contract the repos document in their "Running against fakes" sections.
REPO_SEAMS = {
    "congress-trades": ("MONITOR_CONFIG", "HOUSE_CLERK_BASE_URL", "CONGRESS_TRADES_DATA_DIR"),
    "fare-monitor": ("MONITOR_CONFIG", "FARE_MONITOR_STATE_DIR", "FARE_MONITOR_SOURCES"),
    "value-screener": ("MONITOR_CONFIG", "VALUE_SCREENER_DATA_DIR", "VALUE_SCREENER_OFFLINE"),
}
SEAM_VARS = frozenset(var for seams in REPO_SEAMS.values() for var in seams)

# Captured before any sandbox overrides HOME: a child with HOME moved would otherwise
# look for user-installed packages (defusedxml lives in ~/.local) inside the sandbox.
USER_BASE = site.getuserbase()

PTR_FILING_TYPE = "P"

# A Tuesday NYSE session, so --now exercises a real open rather than a holiday branch.
SMOKE_NOW = "2026-10-13T13:30:00+00:00"
SMOKE_RUNS = [
    ("congress-trades", ["congress_monitor.py"]),
    ("congress-trades", ["watchlist_monitor.py"]),
    ("congress-trades", ["daily_summary.py"]),
    ("fare-monitor", ["fare_monitor.py"]),
    ("value-screener", ["market_open_monitor.py", "--force", "--now", SMOKE_NOW]),
]


class SeamsMissing(RuntimeError):
    """The checkout does not read every seam yet, so running it is not contained."""


def missing_seams(repo_dir: Path, repo: str) -> list[str]:
    """Seam variables no top-level module of the checkout mentions. A plain text search
    is enough: it guards against running a pre-seam checkout, not against a bug in one."""
    source = "".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in sorted(Path(repo_dir).glob("*.py")))
    return [var for var in REPO_SEAMS[repo] if var not in source]


def index_ptr_doc_ids(recordings: Path) -> set[str]:
    """Every PTR DocID in the recorded Clerk index archives."""
    doc_ids = set()
    for archive_path in sorted((recordings / "clerk").glob("public_disc/financial-pdfs/*FD.zip")):
        with zipfile.ZipFile(archive_path) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".txt"))
            lines = archive.read(name).decode("utf-8", errors="replace").splitlines()
        header = lines[0].split("\t")
        for line in lines[1:]:
            fields = line.split("\t")
            if len(fields) == len(header):
                row = dict(zip(header, fields))
                if row.get("FilingType") == PTR_FILING_TYPE:
                    doc_ids.add(row["DocID"])
    return doc_ids


def recorded_pdf_doc_ids(recordings: Path) -> set[str]:
    return {p.stem for p in (recordings / "clerk").glob("public_disc/ptr-pdfs/*/*.pdf")}


@dataclass
class RunResult:
    repo: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    posts: list[dict] = field(default_factory=list)


class Sandbox:
    """A temp dir and a running fake server, wired together.

        with Sandbox() as box:
            box.seed_congress_state()
            result = box.run("congress-trades", ["congress_monitor.py"], repos_dir)
            result.posts   # what Discord would have shown
    """

    def __init__(self, recordings: Path = DEFAULT_RECORDINGS, *, rate_limit_first: int = 0,
                 keep: bool = False):
        self.recordings = Path(recordings).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="factory-sandbox-"))
        self.keep = keep
        self.home = self.root / "home"
        self.posts_path = self.root / "posts.jsonl"
        self.config_path = self.root / "monitor_config.json"
        self.sources_path = self.root / "fare_sources.json"
        self.congress_data = self.root / "congress-trades"
        self.fare_state = self.root / "fare-monitor"
        self.screener_data = self.root / "value-screener"
        self.server = FakeServices(self.recordings, self.posts_path,
                                   rate_limit_first=rate_limit_first)

    # -- setup -----------------------------------------------------------------------

    def start(self) -> "Sandbox":
        self.server.start()
        for directory in (self.home, self.congress_data, self.fare_state, self.screener_data):
            directory.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps({"webhook_url": self.server.webhook_url}))
        self._write_sources()
        self._copy_congress()
        self._copy_yahoo()
        return self

    def _write_sources(self) -> None:
        """The recorded sources manifest with the server's address filled in. With no
        recordings it is an empty list of sources, never the real ones: unset, the seam
        would fall back to the live sites."""
        manifest = self.recordings / "sources" / "sources.json"
        text = (manifest.read_text() if manifest.is_file()
                else json.dumps({"feeds": {}, "pages": {}}))
        self.sources_path.write_text(text.replace("{base}", self.server.sources_base_url))

    def _copy_congress(self) -> None:
        for name in ("leaderboard.json", "watchlist.json"):
            source = self.recordings / "congress" / name
            if source.is_file():
                shutil.copyfile(source, self.congress_data / name)

    def _copy_yahoo(self) -> None:
        """Recorded Yahoo responses become the screener's cache, and its watchlist is
        exactly the recorded tickers, so the offline run has data for every one."""
        cache = self.screener_data / "cache"
        cache.mkdir(exist_ok=True)
        tickers = set()
        for source in sorted((self.recordings / "yahoo").glob("*.json")):
            shutil.copyfile(source, cache / source.name)
            tickers.add(source.name.rsplit(".", 2)[0])
        if tickers:
            (self.screener_data / "watchlist.json").write_text(
                json.dumps({"tickers": sorted(tickers)}, indent=2))

    def seed_congress_state(self, fresh_doc_ids: set[str] | None = None) -> set[str]:
        """Make the congress-trades monitors treat `fresh_doc_ids` as just published.

        Their first run only records a baseline and posts nothing, so to exercise an
        alert the sandbox writes the state an earlier run would have left: every PTR in
        the recorded index already seen except the fresh ones, and every leaderboard and
        watchlist member already baselined. Default: the filings with a recorded PDF.
        Returns the fresh DocIDs."""
        fresh = recorded_pdf_doc_ids(self.recordings) if fresh_doc_ids is None else set(fresh_doc_ids)
        members = set()
        for name in ("leaderboard.json", "watchlist.json"):
            path = self.congress_data / name
            if path.is_file():
                members |= {tuple(m["member_key"]) for m in json.loads(path.read_text())["members"]
                            if m.get("member_key")}
        state = {
            "seen": sorted(index_ptr_doc_ids(self.recordings) - fresh),
            "pending": {},
            "baselined_members": sorted(list(m) for m in members),
        }
        state_dir = self.congress_data / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        for name in ("seen_docids.json", "watchlist_seen_docids.json",
                     "summary_seen_docids.json"):
            (state_dir / name).write_text(json.dumps(state))
        return fresh

    # -- running ---------------------------------------------------------------------

    def env(self, repo: str, base: dict[str, str] | None = None) -> dict[str, str]:
        """The environment that points `repo`'s seams at this sandbox."""
        if repo not in REPO_SEAMS:
            raise ValueError(f"unknown repo {repo!r}; expected one of {sorted(REPO_SEAMS)}")
        env = {k: v for k, v in (os.environ if base is None else base).items()
               if k not in SEAM_VARS}
        env.update({
            "HOME": str(self.home),
            "PYTHONUSERBASE": USER_BASE,
            # The scripts may run from a live checkout; leave no __pycache__ in it.
            "PYTHONDONTWRITEBYTECODE": "1",
            "MONITOR_CONFIG": str(self.config_path),
        })
        if repo == "congress-trades":
            env["HOUSE_CLERK_BASE_URL"] = self.server.clerk_base_url
            env["CONGRESS_TRADES_DATA_DIR"] = str(self.congress_data)
        elif repo == "fare-monitor":
            env["FARE_MONITOR_STATE_DIR"] = str(self.fare_state)
            env["FARE_MONITOR_SOURCES"] = str(self.sources_path)
        else:
            env["VALUE_SCREENER_DATA_DIR"] = str(self.screener_data)
            env["VALUE_SCREENER_OFFLINE"] = "1"
        return env

    def posts(self) -> list[dict]:
        return read_posts(self.posts_path)

    def run(self, repo: str, argv: list[str], repos_dir: Path = DEFAULT_REPOS_DIR,
            timeout: float = 600) -> RunResult:
        """Run `argv[0]` (a script in the repo) with the sandbox environment and return
        its output plus the posts it made. Refuses a checkout that lacks the seams."""
        repo_dir = Path(repos_dir).resolve() / repo
        missing = missing_seams(repo_dir, repo)
        if missing:
            raise SeamsMissing(
                f"{repo_dir} does not read {', '.join(missing)}; running it would touch "
                f"real state. Merge that repo's factory/fakes-seams change first.")
        before = len(self.posts())
        # cwd is the sandbox, so even a stray relative path lands here.
        proc = subprocess.run([sys.executable, str(repo_dir / argv[0]), *argv[1:]],
                              cwd=self.root, env=self.env(repo), capture_output=True,
                              text=True, timeout=timeout)
        return RunResult(repo, list(argv), proc.returncode, proc.stdout, proc.stderr,
                         self.posts()[before:])

    def close(self) -> None:
        self.server.stop()
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


def print_result(result: RunResult, tail: int = 15) -> None:
    print(f"\n=== {result.repo}: {' '.join(result.argv)} -> exit {result.returncode}, "
          f"{len(result.posts)} post(s)")
    output = (result.stdout + result.stderr).strip().splitlines()
    for line in output[-tail:]:
        print(f"  | {line}")
    for number, post in enumerate(result.posts, start=1):
        print(f"  --- post {number} ({len(post['content'])} chars)")
        for line in post["content"].splitlines():
            print(f"  > {line}")


def smoke(repos_dir: Path, recordings: Path, keep: bool) -> int:
    """Each monitor once, against one sandbox, printing what each posted."""
    failed = 0
    with Sandbox(recordings, keep=keep) as box:
        fresh = box.seed_congress_state()
        print(f"Sandbox {box.root}; fake services {box.server.base_url}; congress filings "
              f"staged as new: {', '.join(sorted(fresh)) or 'none'}")
        for repo, argv in SMOKE_RUNS:
            try:
                result = box.run(repo, argv, repos_dir)
            except SeamsMissing as exc:
                print(f"\n=== {repo}: {' '.join(argv)} -> refused: {exc}")
                failed += 1
                continue
            print_result(result)
            failed += result.returncode != 0
    print(f"\n{len(SMOKE_RUNS) - failed}/{len(SMOKE_RUNS)} runs exited 0.")
    return 1 if failed else 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repos-dir", type=Path, default=DEFAULT_REPOS_DIR,
                        help=f"Where the monitor checkouts live (default {DEFAULT_REPOS_DIR})")
    common.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS)
    common.add_argument("--keep", action="store_true",
                        help="Leave the sandbox directory behind for inspection")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke", parents=[common],
                   help="Run each monitor once and print what it posted")
    run = sub.add_parser("run", parents=[common], help="Run one script in a fresh sandbox")
    run.add_argument("repo", choices=sorted(REPO_SEAMS))
    run.add_argument("--seed-congress", action="store_true",
                     help="Stage the recorded PDFs' filings as new (see seed_congress_state)")
    run.add_argument("--rate-limit-first", type=int, default=0, metavar="N")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="script and its arguments")
    args = parser.parse_args()

    if args.command == "smoke":
        return smoke(args.repos_dir, args.recordings, args.keep)
    if not args.argv:
        parser.error("run: name the script to run")
    with Sandbox(args.recordings, rate_limit_first=args.rate_limit_first,
                 keep=args.keep) as box:
        if args.seed_congress:
            box.seed_congress_state()
        try:
            result = box.run(args.repo, args.argv, args.repos_dir)
        except SeamsMissing as exc:
            sys.exit(str(exc))
        print_result(result, tail=1000)
        if args.keep:
            print(f"Sandbox kept at {box.root}")
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
