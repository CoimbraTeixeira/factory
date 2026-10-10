"""Tests for the scenario runner and its input builders.

Run with: python3 -m unittest discover -s tests

No network, no monitor repos, no private scenarios: the Sandbox is replaced by a stub,
so these pin the runner's own contract -- isolation between scenarios, hook order, how
a failure is reported -- and the shapes the builders write.
"""

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scenarios"))

import builders  # noqa: E402
import runner  # noqa: E402
from sandbox import RunResult  # noqa: E402


class StubSandbox:
    """Records what the runner asks of it; each run 'posts' whatever the step names."""
    instances = []

    def __init__(self, recordings, rate_limit_first=0):
        self.recordings, self.rate_limit_first, self.calls = recordings, rate_limit_first, []
        StubSandbox.instances.append(self)

    def __enter__(self):
        self.calls.append("start")
        return self

    def __exit__(self, *exc):
        self.calls.append("stop")

    def run(self, repo, argv, repos_dir):
        self.calls.append(("run", tuple(argv)))
        posts = [{"content": text} for text in argv[1:]]
        return RunResult(repo, list(argv), 0, "", "", posts)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        StubSandbox.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "base"
        (self.base / "sources").mkdir(parents=True)
        (self.base / "sources" / "sources.json").write_text("{}")
        patcher = mock.patch.object(runner, "Sandbox", StubSandbox)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scenario(self, steps, checks, **kwargs):
        return runner.Scenario("s", "fare-monitor", "story", steps, checks, **kwargs)

    def test_hooks_run_in_order_and_checks_see_every_step(self):
        order = []
        scenario = self.scenario(
            [runner.Step(["a.py", "one"]),
             runner.Step(["a.py", "two"], before=lambda ctx: order.append("before-2"))],
            [runner.post_count(2), runner.post_count(1, step=1),
             runner.any_post_contains("two", step=1)],
            setup=lambda ctx: order.append("setup"),
            seed=lambda ctx: order.append("seed"))
        outcome = runner.run_scenario(scenario, self.base, Path("/repos"))
        self.assertTrue(outcome.satisfied, outcome.failures)
        self.assertEqual(order, ["setup", "seed", "before-2"])
        self.assertEqual(StubSandbox.instances[0].calls,
                         ["start", ("run", ("a.py", "one")), ("run", ("a.py", "two")), "stop"])

    def test_each_scenario_gets_its_own_copy_of_the_recordings(self):
        # A scenario that rewrites its inputs must not leak them into the next one.
        def mutate(ctx):
            (ctx.recordings / "sources" / "sources.json").write_text("changed")
        runner.run_scenario(self.scenario([], [], setup=mutate), self.base, Path("/r"))
        self.assertEqual((self.base / "sources" / "sources.json").read_text(), "{}")
        self.assertNotEqual(StubSandbox.instances[0].recordings, self.base)

    def test_failed_check_is_reported_with_its_reason(self):
        outcome = runner.run_scenario(
            self.scenario([runner.Step(["a.py", "hello"])],
                          [runner.post_count(2), runner.no_post_contains("hello")]),
            self.base, Path("/r"))
        self.assertFalse(outcome.satisfied)
        self.assertEqual(outcome.failures[0], "exactly 2 post(s) -- got 1")
        self.assertIn("no post contains 'hello' -- found in:", outcome.failures[1])
        self.assertIn("> hello", outcome.detail)

    def test_a_crashing_hook_fails_the_scenario_instead_of_the_run(self):
        def boom(ctx):
            raise RuntimeError("bad input")
        outcome = runner.run_scenario(self.scenario([], [], setup=boom), self.base, Path("/r"))
        self.assertFalse(outcome.satisfied)
        self.assertIn("RuntimeError: bad input", outcome.detail)

    def test_a_crashing_check_is_a_failure_not_a_crash(self):
        bad = runner.Check("explodes", lambda ctx: 1 / 0)
        outcome = runner.run_scenario(self.scenario([], [bad]), self.base, Path("/r"))
        self.assertIn("explodes -- check raised ZeroDivisionError", outcome.failures[0])

    def test_non_zero_exit_fails_every_step_exits_ok(self):
        ctx = runner.Context(self.base, results=[RunResult("r", ["a.py"], 2, "", "", [])])
        self.assertEqual(runner.every_step_exits_ok().test(ctx), "non-zero exit: [(1, 2)]")


class LoadScenariosTest(unittest.TestCase):
    def write(self, directory, name, ids):
        (directory / name).write_text(
            "from runner import Scenario\n"
            f"SCENARIOS = [Scenario(i, 'fare-monitor', '', [], []) for i in {ids!r}]\n")

    def test_loads_every_module_but_underscored_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.write(directory, "a.py", ["one"])
            self.write(directory, "b.py", ["two"])
            (directory / "_helpers.py").write_text("raise SystemExit('helpers must not load')")
            self.assertEqual([s.id for s in runner.load_scenarios(directory)], ["one", "two"])

    def test_duplicate_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.write(directory, "a.py", ["same"])
            self.write(directory, "b.py", ["same"])
            with self.assertRaises(SystemExit):
                runner.load_scenarios(directory)


class BuildersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.recordings = Path(self.tmp.name)

    def test_rss_items_are_dated_relative_to_now(self):
        root = ET.fromstring(builders.rss_feed([
            {"title": "A & B", "link": "https://x/1", "age_hours": 2, "categories": ["Error Fare"]}]))
        item = root.find("./channel/item")
        self.assertEqual(item.findtext("title"), "A & B")
        age = datetime.now(timezone.utc) - parsedate_to_datetime(item.findtext("pubDate"))
        self.assertAlmostEqual(age.total_seconds() / 3600, 2, delta=0.1)
        self.assertEqual(item.findtext("category"), "Error Fare")

    def test_only_expired_posts_carry_the_stamp(self):
        html = builders.secret_flying_page([
            {"slug": "live", "title": "Live deal"},
            {"slug": "gone", "title": "Gone deal", "expired": True}])
        live, gone = html.split("</article>")[:2]
        self.assertNotIn("expiredStamp", live)
        self.assertIn('class="expiredStamp"', gone)
        self.assertIn('href="https://www.secretflying.com/posts/live/"', live)

    def test_sources_are_replaced_wholesale(self):
        (self.recordings / "sources").mkdir()
        (self.recordings / "sources" / "old.xml").write_text("old")
        builders.write_sources(self.recordings, feeds={"F": ("f.xml", "<rss/>")})
        manifest = json.loads((self.recordings / "sources" / "sources.json").read_text())
        self.assertEqual(manifest, {"feeds": {"F": "{base}/f.xml"}, "pages": {}})
        self.assertFalse((self.recordings / "sources" / "old.xml").exists())

    def test_quote_writes_the_three_cache_files(self):
        builders.write_quote(self.recordings, "DIV01", price=40.0, dividend_rate=0.4)
        files = sorted(p.name for p in (self.recordings / "yahoo").iterdir())
        self.assertEqual(files, ["DIV01.forward.json", "DIV01.quote.json",
                                 "DIV01.statements.json"])
        quote = json.loads((self.recordings / "yahoo" / "DIV01.quote.json").read_text())
        self.assertEqual((quote["currentPrice"], quote["dividendRate"]), (40.0, 0.4))

    def test_pdf_publish_and_withdraw(self):
        source = self.recordings / "src.pdf"
        source.write_bytes(b"%PDF")
        builders.place_pdf(self.recordings, 2026, "123", source)
        target = self.recordings / "clerk/public_disc/ptr-pdfs/2026/123.pdf"
        self.assertTrue(target.exists())
        builders.remove_pdf(self.recordings, 2026, "123")
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
