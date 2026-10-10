#!/usr/bin/env python3
"""Audit the cron logs and turn production anomalies into GitHub issues.

Every bug fixed in these repos so far was found by a person reading logs: a filing
"awaiting a PDF" for 88 hourly runs, scanned filings counted as failures, two cron jobs
pointing at a deleted directory. The unit tests passed throughout. This is that reading,
done nightly, with the findings filed where the work happens -- as issues on the repo
that owns the log, labelled `log-auditor`, which is the factory's work queue.

Findings with no owning repo (a cron entry whose script is gone) go to Discord instead.

Closing an issue means "fixed as of now": a finding reopens its issue only on evidence
newer than the close, so the matches that prompted a fix cannot reopen it while they age
out of the window.

  ./log_auditor.py --dry-run     # print findings; file, post and record nothing
  ./log_auditor.py               # file/reopen issues, post machine findings
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOS_DIR = Path.home() / "Documents" / "repos"
STATE_PATH = Path(__file__).resolve().parent / "state" / "log_auditor.json"
LOCK_PATH = STATE_PATH.with_suffix(".lock")
WEBHOOK_CONFIG = Path.home() / ".config" / "fare-monitor" / "config.json"
LABEL = "log-auditor"
OWNER = "CoimbraTeixeira"

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T[^\]]+)\]")
# A crash's last line: the exception that actually ended the run.
EXCEPTION_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))(?::\s*(.*))?$")
# Outages of someone else's server, not defects here. One is noise; a run of them is a
# finding, because it means a source has been dark for a large part of the day.
TRANSIENT_RE = re.compile(r"ConnectionError|NameResolution|Timeout|TemporaryFailure|"
                          r"RemoteDisconnected|ConnectionReset|MaxRetryError")
TRANSIENT_MIN = 6
VOLATILE_RE = re.compile(r"0x[0-9a-f]+|\d+")
SCRIPT_RE = re.compile(r"(/\S+\.py)\b")


@dataclass
class Pattern:
    """`min_count` matches of `regex` within `window_hours` is a finding. A named group
    `key` splits the count, e.g. per failing source, so one dark site is one issue."""
    name: str
    regex: str
    min_count: int
    window_hours: float
    advice: str


@dataclass
class LastRunNumber:
    """For a log without timestamps: in the last run (from the last `run_start` line),
    the first group of `regex` must not exceed `max_value`."""
    name: str
    run_start: str
    regex: str
    max_value: int
    advice: str


@dataclass
class LogSpec:
    repo: str
    path: Path
    max_silence_hours: float
    patterns: list[Pattern] = field(default_factory=list)
    last_run: list[LastRunNumber] = field(default_factory=list)


DISCORD_FAILURE = Pattern(
    "discord-delivery", r"Failed to (?:send Discord message|post to Discord)", 1, 24,
    "An alert was not delivered. Check the webhook config and the error beside it.")

LOGS = [
    LogSpec(f"{OWNER}/fare-monitor", REPOS_DIR / "fare-monitor" / "monitor.log", 3, [
        Pattern("zero-extraction", r"extracted 0 posts from (?P<key>\S+)", 3, 24,
                "The page layout probably changed; the HTML parser finds no posts."),
        Pattern("source-failing", r"Failed to fetch (?P<key>[^:]+):", 6, 24,
                "A source failed most runs today: blocked, moved, or down."),
        DISCORD_FAILURE,
    ]),
    LogSpec(f"{OWNER}/congress-trades", REPOS_DIR / "congress-trades" / "monitor.log", 3, [
        Pattern("pdf-wait-recurring", r"still awaiting a PDF", 30, 168,
                "Filings keep waiting out the grace period: the publish gap is longer "
                "than expected, or a class of filing is being misread as unpublished."),
        Pattern("alert-backlog", r"deferred\.", 3, 24,
                "--max-alerts is truncating runs repeatedly; check state or raise it."),
        DISCORD_FAILURE,
    ]),
    LogSpec(f"{OWNER}/congress-trades",
            REPOS_DIR / "congress-trades" / "watchlist_monitor.log", 3, [
        Pattern("pdf-wait-recurring", r"still awaiting a PDF", 30, 168,
                "Watchlist filings keep waiting out the grace period."),
        DISCORD_FAILURE,
    ]),
    LogSpec(f"{OWNER}/congress-trades", REPOS_DIR / "congress-trades" / "summary.log", 30, [
        DISCORD_FAILURE,
    ]),
    LogSpec(f"{OWNER}/congress-trades", REPOS_DIR / "congress-trades" / "rebuild.log", 30,
            last_run=[
        LastRunNumber("ptr-failures", r"PTR\(s\) to process", r"PTR\(s\), (\d+) failed", 10,
                      "PTRs failed to fetch or parse in the last rebuild (scans are "
                      "counted separately, so these are real failures)."),
        LastRunNumber("benchmark-unpriced", r"PTR\(s\) to process",
                      r"warning: (SPY) did not price", 0,
                      "The benchmark did not price; every alpha is missing."),
    ]),
    # Weekdays 13:00-15:45 UTC only, so a long weekend is ~117h of legitimate silence.
    LogSpec(f"{OWNER}/value-screener", REPOS_DIR / "value-screener" / "market_open.log", 120, [
        Pattern("no-results", r"No results; not posting", 1, 72,
                "Every ticker failed to fetch at the open; the morning post was skipped."),
    ]),
]


@dataclass
class Line:
    ts: datetime | None
    text: str


@dataclass
class Finding:
    repo: str | None          # None: machine-level, no repo owns it
    check: str
    key: str
    title: str
    body: str
    # Evidence timestamps for count rules, used to decide whether a closed issue has
    # recurred. Empty for state checks (stale log, dead cron target), which recur
    # whenever they are still true.
    times: list[datetime] = field(default_factory=list)
    min_count: int = 1

    @property
    def fingerprint(self) -> str:
        return f"{self.repo or 'machine'}:{self.check}:{self.key}"


def parse_ts(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def parse_log(text: str) -> list[Line]:
    """Lines with the timestamp of the last stamped line at or before them. Tracebacks
    and stderr warnings carry no stamp of their own; they belong to the run after it."""
    lines, current = [], None
    for raw in text.splitlines():
        match = TS_RE.match(raw)
        if match:
            current = parse_ts(match.group(1)) or current
        lines.append(Line(current, raw))
    return lines


def crash_signatures(lines: list[Line]) -> list[tuple[str, datetime | None, str]]:
    """(signature, time, exception line) per crash. A crash is a block of unstamped
    lines containing a traceback; chained exceptions share one block, and the last
    exception line in it is what ended the run."""
    crashes, block, block_ts = [], [], None

    def close():
        if any(line.startswith("Traceback") for line in block):
            final = next((line for line in reversed(block) if EXCEPTION_RE.match(line)), None)
            if final:
                exc, message = EXCEPTION_RE.match(final).groups()
                signature = exc + ": " + VOLATILE_RE.sub("#", message or "")[:90]
                crashes.append((signature, block_ts, final))

    for line in lines:
        if TS_RE.match(line.text):
            close()
            block, block_ts = [], line.ts
        else:
            block.append(line.text)
    close()
    return crashes


def check_stale(spec: LogSpec, now: datetime) -> list[Finding]:
    if not spec.path.exists():
        return [Finding(spec.repo, "log-missing", spec.path.name,
                        f"{spec.path.name} does not exist",
                        f"`{spec.path}` is missing, so its cron job has never written "
                        f"output here, or the log was moved.")]
    age = now - datetime.fromtimestamp(spec.path.stat().st_mtime, timezone.utc)
    if age <= timedelta(hours=spec.max_silence_hours):
        return []
    return [Finding(spec.repo, "log-silent", spec.path.name,
                    f"{spec.path.name} silent for {age.total_seconds() / 3600:.0f}h",
                    f"`{spec.path}` was last written {age.total_seconds() / 3600:.0f} "
                    f"hours ago; its job should write at least every "
                    f"{spec.max_silence_hours:g}h. The cron entry may be missing or "
                    f"the job may be hanging before it logs.")]


def check_patterns(spec: LogSpec, lines: list[Line], now: datetime) -> list[Finding]:
    findings = []
    for pattern in spec.patterns:
        regex = re.compile(pattern.regex)
        since = now - timedelta(hours=pattern.window_hours)
        hits: dict[str, list[Line]] = defaultdict(list)
        for line in lines:
            match = regex.search(line.text)
            if match and line.ts and line.ts >= since:
                hits[match.groupdict().get("key") or ""].append(line)
        for key, matched in hits.items():
            if len(matched) < pattern.min_count:
                continue
            label = f" ({key})" if key else ""
            findings.append(Finding(
                spec.repo, pattern.name, key or spec.path.name,
                f"{pattern.name}{label}: {len(matched)}x in {pattern.window_hours:g}h",
                f"{pattern.advice}\n\n`{spec.path.name}` matched `{pattern.regex}` "
                f"{len(matched)} times in the last {pattern.window_hours:g}h "
                f"(threshold {pattern.min_count}). Latest:\n\n```\n"
                + "\n".join(m.text[:200] for m in matched[-5:]) + "\n```",
                times=[m.ts for m in matched], min_count=pattern.min_count))
    return findings


def check_crashes(spec: LogSpec, lines: list[Line], now: datetime) -> list[Finding]:
    since = now - timedelta(hours=24)
    by_signature: dict[str, list[tuple[datetime | None, str]]] = defaultdict(list)
    for signature, ts, final in crash_signatures(lines):
        if ts is None or ts >= since:
            by_signature[signature].append((ts, final))
    findings = []
    for signature, crashes in by_signature.items():
        transient = bool(TRANSIENT_RE.search(signature))
        threshold = TRANSIENT_MIN if transient else 1
        if len(crashes) < threshold:
            continue
        kind = "network outage" if transient else "crash"
        findings.append(Finding(
            spec.repo, "crash", signature,
            f"{kind}: {signature.split(':')[0]} {len(crashes)}x in 24h ({spec.path.name})",
            f"`{spec.path.name}` ended {len(crashes)} run(s) in the last 24h with:\n\n```\n"
            f"{crashes[-1][1][:400]}\n```\n\n"
            + ("Repeated network failures: a source has been unreachable for much of "
               "the day." if transient else
               "An uncaught exception ended the run; nothing after it was processed."),
            times=[ts for ts, _ in crashes if ts], min_count=threshold))
    return findings


def check_last_run(spec: LogSpec, lines: list[Line]) -> list[Finding]:
    # The log has no timestamps, so the run is dated by the file's last write: that is
    # what lets a closed issue tell the run it was closed over from a newer one.
    written = datetime.fromtimestamp(spec.path.stat().st_mtime, timezone.utc)
    findings = []
    for rule in spec.last_run:
        starts = [i for i, line in enumerate(lines) if re.search(rule.run_start, line.text)]
        run = lines[starts[-1]:] if starts else lines
        for line in run:
            match = re.search(rule.regex, line.text)
            if not match:
                continue
            value = int(match.group(1)) if match.group(1).isdigit() else 1
            if value > rule.max_value:
                findings.append(Finding(
                    spec.repo, rule.name, spec.path.name,
                    f"{rule.name}: {line.text.strip()[:80]}",
                    f"{rule.advice}\n\nLast run of `{spec.path.name}`:\n\n```\n"
                    f"{line.text.strip()}\n```",
                    times=[written]))
    return findings


def check_cron(crontab: str) -> list[Finding]:
    """Cron entries whose script no longer exists fail on every tick, and cron's error
    mail goes nowhere on most desktops -- so nothing else would ever notice."""
    findings = []
    for entry in crontab.splitlines():
        if not entry.strip() or entry.lstrip().startswith("#"):
            continue
        for script in SCRIPT_RE.findall(entry):
            if not Path(script).exists():
                findings.append(Finding(
                    None, "cron-dead-target", script,
                    f"cron runs a missing script: {script}",
                    f"Every run of this cron entry fails:\n`{entry.strip()}`\n"
                    f"Remove the entry (`crontab -e`) or restore the script."))
    return findings


def audit(now: datetime, crontab: str) -> list[Finding]:
    findings = check_cron(crontab)
    for spec in LOGS:
        findings += check_stale(spec, now)
        if not spec.path.exists():
            continue
        lines = parse_log(spec.path.read_text(errors="replace"))
        findings += check_patterns(spec, lines, now)
        findings += check_crashes(spec, lines, now)
        findings += check_last_run(spec, lines)
    return findings


# --- Delivery --------------------------------------------------------------------------


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def recurred_since(finding: Finding, closed_at: datetime | None) -> bool:
    """A closed issue is "fixed as of closing"; only newer evidence reopens it."""
    if closed_at is None or not finding.times:
        return True
    return sum(1 for t in finding.times if t > closed_at) >= finding.min_count


def file_issue(finding: Finding, state: dict, dry_run: bool) -> str:
    known = state["issues"].get(finding.fingerprint)
    body = finding.body + f"\n\n<sub>{LABEL} fingerprint: `{finding.fingerprint}`</sub>"
    if known:
        info = json.loads(gh("issue", "view", str(known), "-R", finding.repo,
                             "--json", "state,closedAt"))
        if info["state"] == "OPEN":
            return f"open #{known}"
        if not recurred_since(finding, parse_ts(info["closedAt"] or "")):
            return f"closed #{known}, no newer evidence"
        if not dry_run:
            gh("issue", "reopen", str(known), "-R", finding.repo, "--comment",
               "Recurred after closing.\n\n" + body)
        return f"reopened #{known}"
    if dry_run:
        return "would open"
    gh("label", "create", LABEL, "-R", finding.repo, "--color", "BFD4F2", "--force",
       "--description", "Opened by factory/log_auditor.py from cron logs")
    url = gh("issue", "create", "-R", finding.repo, "--title", f"[{LABEL}] {finding.title}",
             "--body", body, "--label", LABEL).strip()
    state["issues"][finding.fingerprint] = int(url.rsplit("/", 1)[-1])
    return f"opened {url}"


def notify_machine(findings: list[Finding], state: dict, dry_run: bool) -> list[str]:
    """Post machine findings once each; a finding that clears is forgotten, so it is
    posted again if it ever returns."""
    current = {f.fingerprint for f in findings}
    state["notified"] = {fp: at for fp, at in state["notified"].items() if fp in current}
    fresh = [f for f in findings if f.fingerprint not in state["notified"]]
    if not fresh or dry_run:
        return [f"{'would post' if dry_run else 'already posted'}: {f.title}" for f in fresh]
    text = "🛠️ **Log auditor** — machine-level findings\n" + "\n".join(
        f"- {f.title}\n  {f.body.splitlines()[1] if len(f.body.splitlines()) > 1 else ''}"
        for f in fresh)
    url = json.loads(WEBHOOK_CONFIG.read_text())["webhook_url"]
    request = urllib.request.Request(url, data=json.dumps({"content": text[:1990]}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "factory-log-auditor/1.0"})
    urllib.request.urlopen(request, timeout=30).close()
    stamp = datetime.now(timezone.utc).isoformat()
    for f in fresh:
        state["notified"][f.fingerprint] = stamp
    return [f"posted: {f.title}" for f in fresh]


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    return {"issues": state.get("issues", {}), "notified": state.get("notified", {})}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, STATE_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="print findings; file, post and record nothing")
    args = parser.parse_args()

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another audit is running; skipping.")
            return 0
        now = datetime.now(timezone.utc)
        crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        findings = audit(now, crontab)
        state = load_state()
        print(f"[{now.isoformat()}] {len(findings)} finding(s).")
        for finding in (f for f in findings if f.repo):
            try:
                outcome = file_issue(finding, state, args.dry_run)
            except subprocess.CalledProcessError as exc:
                outcome = f"gh failed: {exc.stderr.strip()[:200]}"
            print(f"  {finding.repo}: {finding.title} -> {outcome}")
        for line in notify_machine([f for f in findings if not f.repo], state, args.dry_run):
            print(f"  machine: {line}")
        if not args.dry_run:
            save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
