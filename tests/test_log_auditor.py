"""Tests for the log auditor: the rules that decide a finding, and the issue lifecycle.

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import log_auditor as la  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def stamp(hours_ago: float) -> str:
    return f"[{(NOW - timedelta(hours=hours_ago)).isoformat()}]"


def spec_for(path: Path, **kwargs) -> la.LogSpec:
    return la.LogSpec("owner/repo", path, kwargs.pop("max_silence_hours", 3), **kwargs)


TRACEBACK = """Traceback (most recent call last):
  File "x.py", line 1, in <module>
socket.gaierror: [Errno -3] Temporary failure in name resolution

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "y.py", line 2, in run
requests.exceptions.ConnectionError: HTTPSConnectionPool(host='a.gov', port=443): Max retries
"""


class ParseLogTest(unittest.TestCase):
    def test_unstamped_lines_inherit_the_last_stamp(self):
        lines = la.parse_log(f"{stamp(2)} run one\nWarning: extracted 0 posts\n{stamp(1)} two")
        self.assertEqual([line.ts for line in lines],
                         [NOW - timedelta(hours=2)] * 2 + [NOW - timedelta(hours=1)])

    def test_lines_before_any_stamp_have_none(self):
        self.assertIsNone(la.parse_log("Parsed 5 lines\n")[0].ts)


class CrashTest(unittest.TestCase):
    def test_chained_traceback_is_one_crash_named_by_its_last_exception(self):
        crashes = la.crash_signatures(la.parse_log(f"{stamp(3)} ok\n{TRACEBACK}{stamp(2)} ok"))
        self.assertEqual(len(crashes), 1)
        self.assertTrue(crashes[0][0].startswith("requests.exceptions.ConnectionError"))

    def test_signature_ignores_volatile_numbers(self):
        a = la.crash_signatures(la.parse_log("Traceback\nKeyError: 'doc 123' at 0x7f00"))
        b = la.crash_signatures(la.parse_log("Traceback\nKeyError: 'doc 456' at 0x7fff"))
        self.assertEqual(a[0][0], b[0][0])

    def test_one_real_crash_is_a_finding(self):
        text = f"{stamp(5)} ok\nTraceback\nKeyError: 'DocID'\n"
        findings = la.check_crashes(spec_for(Path("m.log")), la.parse_log(text), NOW)
        self.assertEqual([f.check for f in findings], ["crash"])

    def test_a_single_network_blip_is_not_a_finding(self):
        # A DNS outage ended runs across three monitors on 2026-09-28; one is noise.
        text = f"{stamp(5)} ok\n{TRACEBACK}"
        self.assertEqual(la.check_crashes(spec_for(Path("m.log")), la.parse_log(text), NOW), [])

    def test_a_day_of_network_failures_is_a_finding(self):
        text = "".join(f"{stamp(h)} ok\n{TRACEBACK}" for h in range(1, la.TRANSIENT_MIN + 1))
        findings = la.check_crashes(spec_for(Path("m.log")), la.parse_log(text), NOW)
        self.assertEqual(len(findings), 1)
        self.assertIn("network outage", findings[0].title)

    def test_crashes_older_than_a_day_are_ignored(self):
        text = f"{stamp(30)} ok\nTraceback\nKeyError: 'DocID'\n{stamp(1)} ok"
        self.assertEqual(la.check_crashes(spec_for(Path("m.log")), la.parse_log(text), NOW), [])


class PatternTest(unittest.TestCase):
    PATTERN = la.Pattern("pdf-wait", r"still awaiting a PDF", 3, 24, "advice")

    def run_check(self, text, pattern=None):
        spec = spec_for(Path("m.log"), patterns=[pattern or self.PATTERN])
        return la.check_patterns(spec, la.parse_log(text), NOW)

    def test_threshold_must_be_met_inside_the_window(self):
        recent = "".join(f"{stamp(h)} 1 still awaiting a PDF.\n" for h in (1, 2))
        old = f"{stamp(40)} 1 still awaiting a PDF.\n"
        self.assertEqual(self.run_check(old + recent), [])
        self.assertEqual(len(self.run_check(old + recent + f"{stamp(3)} still awaiting a PDF\n")), 1)

    def test_key_group_splits_findings_per_source(self):
        pattern = la.Pattern("source-failing", r"Failed to fetch (?P<key>[^:]+):", 2, 24, "a")
        text = "".join(f"{stamp(h)} Failed to fetch {site}: 403\n"
                       for h in (1, 2) for site in ("Fly4Free", "Travelfree"))
        findings = self.run_check(text, pattern)
        self.assertEqual(sorted(f.key for f in findings), ["Fly4Free", "Travelfree"])

    def test_finding_carries_its_evidence_times(self):
        text = "".join(f"{stamp(h)} still awaiting a PDF\n" for h in (1, 2, 3))
        self.assertEqual(len(self.run_check(text)[0].times), 3)


class LastRunTest(unittest.TestCase):
    RULE = la.LastRunNumber("ptr-failures", r"PTR\(s\) to process",
                            r"PTR\(s\), (\d+) failed", 10, "advice")

    def check(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rebuild.log"
            path.write_text(text)
            return la.check_last_run(spec_for(path, last_run=[self.RULE]), la.parse_log(text))

    def test_only_the_last_run_counts(self):
        # The 247 "failures" were scans, recounted separately from the next rebuild on;
        # an old run must not keep the finding alive.
        text = ("9 PTR(s) to process\nParsed 1 line(s) from 9 PTR(s), 247 failed.\n"
                "9 PTR(s) to process\nParsed 1 line(s) from 9 PTR(s).\n")
        self.assertEqual(self.check(text), [])

    def test_last_run_over_the_limit_is_dated_by_the_log(self):
        findings = self.check("9 PTR(s) to process\nParsed 1 line(s) from 9 PTR(s), 11 failed.\n")
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(findings[0].times), 1)


class StaleAndCronTest(unittest.TestCase):
    def test_silent_log_is_a_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.log"
            path.write_text("x")
            old = (NOW - timedelta(hours=5)).timestamp()
            os.utime(path, (old, old))
            self.assertEqual([f.check for f in la.check_stale(spec_for(path), NOW)],
                             ["log-silent"])
            self.assertEqual(la.check_stale(spec_for(path, max_silence_hours=6), NOW), [])

    def test_cron_entry_for_a_deleted_script_is_a_machine_finding(self):
        with tempfile.NamedTemporaryFile(suffix=".py") as live:
            crontab = (f"# comment /gone/old.py\n"
                       f"0 * * * * /usr/bin/python3 {live.name} >> x.log\n"
                       f"0 9 * * 1 /usr/bin/python3 /gone/weekly_summary.py >> w.log\n")
            findings = la.check_cron(crontab)
        self.assertEqual([f.key for f in findings], ["/gone/weekly_summary.py"])
        self.assertIsNone(findings[0].repo)


class IssueLifecycleTest(unittest.TestCase):
    def finding(self, times):
        return la.Finding("owner/repo", "pdf-wait", "m.log", "t", "b", times=times, min_count=2)

    def test_closing_means_fixed_as_of_then(self):
        closed = NOW - timedelta(hours=1)
        old = [NOW - timedelta(hours=h) for h in (3, 4, 5)]
        self.assertFalse(la.recurred_since(self.finding(old), closed))
        self.assertTrue(la.recurred_since(self.finding(old + [NOW, NOW]), closed))

    def gh_stub(self, view_state):
        calls = []

        def fake(*args):
            calls.append(args[:2])
            if args[:2] == ("issue", "view"):
                return json.dumps(view_state)
            if args[:2] == ("issue", "create"):
                return "https://github.com/owner/repo/issues/42\n"
            return ""
        return calls, fake

    def test_new_finding_opens_an_issue_and_remembers_it(self):
        calls, fake = self.gh_stub({})
        state = {"issues": {}, "notified": {}}
        with mock.patch.object(la, "gh", fake):
            outcome = la.file_issue(self.finding([NOW]), state, dry_run=False)
        self.assertIn("issues/42", outcome)
        self.assertEqual(state["issues"]["owner/repo:pdf-wait:m.log"], 42)

    def test_open_issue_is_left_alone(self):
        calls, fake = self.gh_stub({"state": "OPEN", "closedAt": None})
        state = {"issues": {"owner/repo:pdf-wait:m.log": 7}, "notified": {}}
        with mock.patch.object(la, "gh", fake):
            self.assertEqual(la.file_issue(self.finding([NOW]), state, False), "open #7")
        self.assertNotIn(("issue", "reopen"), calls)

    def test_closed_issue_reopens_only_on_newer_evidence(self):
        closed = (NOW - timedelta(hours=1)).isoformat()
        calls, fake = self.gh_stub({"state": "CLOSED", "closedAt": closed})
        state = {"issues": {"owner/repo:pdf-wait:m.log": 7}, "notified": {}}
        with mock.patch.object(la, "gh", fake):
            stale = la.file_issue(self.finding([NOW - timedelta(hours=3)] * 2), state, False)
            fresh = la.file_issue(self.finding([NOW, NOW]), state, False)
        self.assertIn("no newer evidence", stale)
        self.assertEqual(fresh, "reopened #7")
        self.assertEqual(calls.count(("issue", "reopen")), 1)

    def test_dry_run_changes_nothing(self):
        calls, fake = self.gh_stub({})
        state = {"issues": {}, "notified": {}}
        with mock.patch.object(la, "gh", fake):
            self.assertEqual(la.file_issue(self.finding([NOW]), state, True), "would open")
        self.assertEqual((calls, state["issues"]), ([], {}))


class MachineNotifyTest(unittest.TestCase):
    def test_cleared_findings_are_forgotten_so_a_return_is_posted_again(self):
        state = {"issues": {}, "notified": {"machine:cron-dead-target:/gone.py": "x"}}
        la.notify_machine([], state, dry_run=True)
        self.assertEqual(state["notified"], {})


if __name__ == "__main__":
    unittest.main()
