#!/usr/bin/env python3
"""Run the hidden end-to-end scenarios against the fakes and report satisfaction.

A scenario is a user story with an observable outcome: "a scanned paper filing gets
exactly one alert, and it says paper", "an expired error fare is never alerted". It runs
the real monitor scripts from the live checkouts, inside a sandbox (fakes/sandbox.py),
against inputs built for it, and checks what Discord would have received.

The scenarios themselves are NOT in this repo. They live in a private directory
(default ~/factory-scenarios, or --scenarios-dir / FACTORY_SCENARIOS_DIR), outside every
checkout a coding agent works in. Tests an agent can read are tests it can satisfy
without making the code right; these are the held-out set, like a model's test data.

Satisfaction is the fraction of scenarios whose every check passed. Below
--min-satisfaction (default 1.0) the run exits 1, and --notify posts the failing
scenario IDs (never their contents) to the Discord webhook.

  ./scenarios/runner.py                    # every scenario
  ./scenarios/runner.py --only fare-       # IDs containing "fare-"
  ./scenarios/runner.py --notify           # what the nightly cron runs
"""

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import traceback
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

FACTORY_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FACTORY_DIR / "fakes"))

from fake_services import DEFAULT_RECORDINGS  # noqa: E402
from sandbox import DEFAULT_REPOS_DIR, RunResult, Sandbox  # noqa: E402

DEFAULT_SCENARIOS_DIR = Path(os.environ.get("FACTORY_SCENARIOS_DIR")
                             or Path.home() / "factory-scenarios")
REPORT_PATH = FACTORY_DIR / "state" / "scenarios-report.json"
WEBHOOK_CONFIG = Path.home() / ".config" / "fare-monitor" / "config.json"


@dataclass
class Context:
    """What a scenario's hooks and checks see: its private copy of the recordings, the
    running sandbox, and one RunResult per step so far."""
    recordings: Path
    box: Sandbox | None = None
    results: list[RunResult] = field(default_factory=list)

    def posts(self, step: int | None = None) -> list[str]:
        """Post bodies Discord received, from one step or (default) all of them."""
        chosen = self.results if step is None else [self.results[step]]
        return [post["content"] for result in chosen for post in result.posts]


@dataclass
class Step:
    argv: list[str]
    before: Callable[[Context], None] | None = None


@dataclass
class Check:
    description: str
    # True passes. False fails. A string fails and says why.
    test: Callable[[Context], bool | str]


@dataclass
class Scenario:
    id: str
    repo: str
    story: str
    steps: list[Step]
    checks: list[Check]
    # Before the sandbox starts: write inputs into ctx.recordings.
    setup: Callable[[Context], None] | None = None
    # After it starts, before the first step: e.g. seed monitor state.
    seed: Callable[[Context], None] | None = None
    rate_limit_first: int = 0


@dataclass
class Outcome:
    id: str
    satisfied: bool
    failures: list[str] = field(default_factory=list)
    detail: str = ""


# --- Check helpers ----------------------------------------------------------------------


def _where(step: int | None) -> str:
    return "" if step is None else f" in step {step + 1}"


def post_count(n: int, step: int | None = None) -> Check:
    def test(ctx: Context) -> bool | str:
        got = len(ctx.posts(step))
        return got == n or f"got {got}"
    return Check(f"exactly {n} post(s){_where(step)}", test)


def any_post_contains(text: str, step: int | None = None) -> Check:
    return Check(f"a post contains {text!r}{_where(step)}",
                 lambda ctx: any(text in post for post in ctx.posts(step)))


def no_post_contains(text: str, step: int | None = None) -> Check:
    def test(ctx: Context) -> bool | str:
        hits = [post for post in ctx.posts(step) if text in post]
        return not hits or f"found in: {hits[0][:120]!r}"
    return Check(f"no post contains {text!r}{_where(step)}", test)


def every_step_exits_ok() -> Check:
    def test(ctx: Context) -> bool | str:
        bad = [(i + 1, r.returncode) for i, r in enumerate(ctx.results) if r.returncode != 0]
        return not bad or f"non-zero exit: {bad}"
    return Check("every step exits 0", test)


# --- Loading and running ----------------------------------------------------------------


def load_scenarios(directory: Path) -> list[Scenario]:
    """Every SCENARIOS list in the directory's *.py modules, ID-unique."""
    if not directory.is_dir():
        sys.exit(f"No scenarios directory at {directory} (set --scenarios-dir or "
                 f"FACTORY_SCENARIOS_DIR).")
    sys.path.insert(0, str(directory))
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"scenarios_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        scenarios += getattr(module, "SCENARIOS", [])
    ids = [s.id for s in scenarios]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        sys.exit(f"Duplicate scenario IDs: {', '.join(duplicates)}")
    return scenarios


def run_scenario(scenario: Scenario, base_recordings: Path, repos_dir: Path) -> Outcome:
    """Run one scenario in its own copy of the recordings and its own sandbox, so no
    scenario's inputs or state can leak into another's."""
    with tempfile.TemporaryDirectory(prefix=f"scenario-{scenario.id}-") as tmp:
        recordings = Path(tmp) / "recordings"
        shutil.copytree(base_recordings, recordings)
        ctx = Context(recordings)
        try:
            if scenario.setup:
                scenario.setup(ctx)
            with Sandbox(recordings, rate_limit_first=scenario.rate_limit_first) as box:
                ctx.box = box
                if scenario.seed:
                    scenario.seed(ctx)
                for step in scenario.steps:
                    if step.before:
                        step.before(ctx)
                    ctx.results.append(box.run(scenario.repo, step.argv, repos_dir))
        except Exception:
            return Outcome(scenario.id, False, ["scenario did not run"], traceback.format_exc())

        failures = []
        for check in scenario.checks:
            try:
                verdict = check.test(ctx)
            except Exception as exc:
                verdict = f"check raised {type(exc).__name__}: {exc}"
            if verdict is not True:
                failures.append(check.description
                                + (f" -- {verdict}" if isinstance(verdict, str) else ""))
        detail = "" if not failures else _transcript(ctx)
        return Outcome(scenario.id, not failures, failures, detail)


def _transcript(ctx: Context) -> str:
    """What a failing scenario's steps printed and posted, for diagnosis."""
    lines = []
    for i, result in enumerate(ctx.results, start=1):
        lines.append(f"  step {i}: {' '.join(result.argv)} -> exit {result.returncode}")
        for stream in (result.stdout, result.stderr):
            lines += [f"    | {line}" for line in stream.strip().splitlines()[-6:]]
        for post in result.posts:
            lines += [f"    > {line}" for line in post["content"].splitlines()[:8]]
    return "\n".join(lines)


def notify_failures(outcomes: list[Outcome], satisfaction: float) -> None:
    """Failing IDs only: the channel learns that something regressed, while what the
    scenario checks stays out of anything a coding agent might later read."""
    failing = ", ".join(o.id for o in outcomes if not o.satisfied)
    text = (f"🧪 **Scenarios** — satisfaction {satisfaction:.0%}. Failing: {failing}. "
            f"Run `factory/scenarios/runner.py --only <id>` for details.")
    url = json.loads(WEBHOOK_CONFIG.read_text())["webhook_url"]
    request = urllib.request.Request(url, data=json.dumps({"content": text[:1990]}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "factory-scenarios/1.0"})
    urllib.request.urlopen(request, timeout=30).close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenarios-dir", type=Path, default=DEFAULT_SCENARIOS_DIR)
    parser.add_argument("--repos-dir", type=Path, default=DEFAULT_REPOS_DIR)
    parser.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS)
    parser.add_argument("--only", action="append", default=[],
                        help="run scenarios whose ID contains this (repeatable)")
    parser.add_argument("--min-satisfaction", type=float, default=1.0)
    parser.add_argument("--notify", action="store_true",
                        help="post failing scenario IDs to Discord when below the bar")
    parser.add_argument("--report", type=Path, default=REPORT_PATH,
                        help="where to write the JSON report (the PR check runs the "
                             "factory checkout read-only)")
    args = parser.parse_args()

    scenarios = [s for s in load_scenarios(args.scenarios_dir)
                 if not args.only or any(part in s.id for part in args.only)]
    if not scenarios:
        sys.exit("No scenarios matched.")
    started = datetime.now(timezone.utc)
    outcomes = []
    for scenario in scenarios:
        outcome = run_scenario(scenario, args.recordings, args.repos_dir)
        outcomes.append(outcome)
        print(f"{'PASS' if outcome.satisfied else 'FAIL'}  {scenario.id}")
        for failure in outcome.failures:
            print(f"      - {failure}")
        if outcome.detail:
            print(outcome.detail)

    satisfied = sum(o.satisfied for o in outcomes)
    satisfaction = satisfied / len(outcomes)
    print(f"\nSatisfaction: {satisfied}/{len(outcomes)} ({satisfaction:.0%}) "
          f"in {(datetime.now(timezone.utc) - started).total_seconds():.0f}s")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "run_at": started.isoformat(), "satisfaction": satisfaction,
        "outcomes": [{"id": o.id, "satisfied": o.satisfied, "failures": o.failures}
                     for o in outcomes]}, indent=2))
    below = satisfaction < args.min_satisfaction
    if below and args.notify:
        notify_failures(outcomes, satisfaction)
    return 1 if below else 0


if __name__ == "__main__":
    sys.exit(main())
