"""Tests for the agent harness, the model relay and the PR scenario status.

Run with: python3 -m unittest discover -s tests

No Docker, no network, no omp: the gateway is a stub process speaking the stdio JSON-lines
protocol, and `gh` is stubbed. These pin the decisions that keep the agent contained and
honest -- relay auth and routing, work selection limits, refusing changes to the gates,
and statuses that never name a scenario.
"""

import io
import json
import sys
import tarfile
import tempfile
import textwrap
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))

import agent_harness as ah  # noqa: E402
import gateway_relay as gr  # noqa: E402
import scenario_status as ss  # noqa: E402

# Answers each request with its own body, so a test can see exactly what was forwarded.
ECHO_GATEWAY = textwrap.dedent("""
    import json, sys
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        print(json.dumps({"id": req["id"], "status": 200,
                          "body": "data: " + json.dumps(req["body"]) + "\\n\\n"}), flush=True)
""")


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.relay = gr.Relay("127.0.0.1", gateway_command=[sys.executable, "-c", ECHO_GATEWAY])
        self.relay.__enter__()
        self.addCleanup(self.relay.__exit__)

    def post(self, path, body, token=None):
        request = urllib.request.Request(
            self.relay.base_url + path, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token or self.relay.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers["Content-Type"], response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers["Content-Type"], exc.read().decode()

    def test_stream_is_forwarded_as_sse_with_model_renamed(self):
        status, ctype, text = self.post("/v1/pi/stream",
                                        {"modelId": "anthropic/x", "context": {"messages": []}})
        self.assertEqual((status, ctype), (200, "text/event-stream"))
        forwarded = json.loads(text.removeprefix("data: "))
        self.assertEqual(forwarded["model"], "anthropic/x")
        self.assertNotIn("modelId", forwarded)

    def test_wrong_token_is_refused(self):
        self.assertEqual(self.post("/v1/pi/stream", {}, token="guess")[0], 401)

    def test_only_the_model_route_is_relayed(self):
        # Usage, credentials and the rest of the gateway stay unreachable.
        self.assertEqual(self.post("/v1/credentials/check", {})[0], 404)

    def test_tokens_differ_per_relay(self):
        other = gr.Relay("127.0.0.1", gateway_command=[sys.executable, "-c", ECHO_GATEWAY])
        try:
            self.assertNotEqual(other.token, self.relay.token)
        finally:
            other.server.server_close()
            other.gateway.close()


class SelectionTest(unittest.TestCase):
    NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)

    def gh_stub(self, issues_by_repo, open_pr_branches=()):
        def fake(*args):
            repo = args[args.index("-R") + 1].split("/")[1]
            if args[:2] == ("issue", "list"):
                return issues_by_repo.get(repo, [])
            if args[:2] == ("pr", "list"):
                branch = args[args.index("--head") + 1]
                return [{"number": 1}] if (repo, branch) in open_pr_branches else []
            raise AssertionError(args)
        return fake

    def issue(self, number, created):
        return {"number": number, "title": f"t{number}", "body": "", "url": "",
                "createdAt": created}

    def test_oldest_first_across_repos(self):
        stub = self.gh_stub({"fare-monitor": [self.issue(5, "2026-10-09T00:00:00Z")],
                             "value-screener": [self.issue(3, "2026-10-01T00:00:00Z")]})
        with mock.patch.object(ah, "gh_json", stub):
            picked = ah.candidate_issues({"attempts": {}, "runs": []})
        self.assertEqual([(i["repo"], i["number"]) for i in picked],
                         [("value-screener", 3), ("fare-monitor", 5)])

    def test_exhausted_and_in_review_issues_are_skipped(self):
        stub = self.gh_stub({"fare-monitor": [self.issue(5, "a"), self.issue(6, "b")]},
                            open_pr_branches={("fare-monitor", "agent/issue-6")})
        state = {"attempts": {"fare-monitor#5": ah.MAX_ATTEMPTS}, "runs": []}
        with mock.patch.object(ah, "gh_json", stub):
            self.assertEqual(ah.candidate_issues(state), [])

    def test_daily_limit_counts_runs_started_today(self):
        state = {"attempts": {}, "runs": [{"started_at": "2026-10-10T01:00:00+00:00"},
                                          {"started_at": "2026-10-09T23:00:00+00:00"}]}
        self.assertEqual(ah.runs_today(state, self.NOW), 1)


class PromptAndGuardTest(unittest.TestCase):
    def test_prompt_carries_the_issue_and_the_repo_gates(self):
        prompt = ah.build_prompt({"repo": "fare-monitor", "number": 7,
                                  "title": "Skip expired deals", "body": "They alert."})
        self.assertIn("# #7: Skip expired deals", prompt)
        self.assertIn("They alert.", prompt)
        self.assertIn("python3 -m unittest test_fare_monitor", prompt)
        self.assertIn("python3 hooks/pre-commit --all", prompt)
        self.assertIn("agent/issue-7", prompt)

    def test_changes_to_the_gates_are_refused(self):
        files = ["fare_monitor.py", ".github/workflows/ci.yml", "hooks/pre-commit",
                 "ruff.toml", "tests/test_x.py"]
        self.assertEqual(ah.protected_changes(files),
                         [".github/workflows/ci.yml", "hooks/pre-commit", "ruff.toml"])

    def test_long_agent_output_keeps_the_end(self):
        # The PR description is the agent's closing summary, at the end of its output.
        text = ah.summary_of("x" * 10000 + "SUMMARY", limit=100)
        self.assertTrue(text.endswith("SUMMARY"))
        self.assertLess(len(text), 110)


class ScenarioStatusTest(unittest.TestCase):
    def test_description_counts_without_naming_scenarios(self):
        report = {"outcomes": [{"id": "fare-expired-never-alerts", "satisfied": False},
                               {"id": "b", "satisfied": True}]}
        state, description = ss.describe(report)
        self.assertEqual((state, description), ("failure", "1/2 hidden scenarios satisfied"))
        self.assertNotIn("fare-expired", description)

    def test_all_satisfied_is_success_and_none_is_error(self):
        self.assertEqual(ss.describe({"outcomes": [{"id": "a", "satisfied": True}]})[0],
                         "success")
        self.assertEqual(ss.describe({"outcomes": []})[0], "error")

    def test_tarball_top_directory_is_stripped(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in (("Owner-repo-abc/README.md", b"hi"),
                               ("Owner-repo-abc/tests/t.py", b"x")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        with tempfile.TemporaryDirectory() as tmp:
            ss.extract_tarball(buffer.getvalue(), Path(tmp) / "repo")
            self.assertEqual((Path(tmp) / "repo" / "README.md").read_text(), "hi")
            self.assertTrue((Path(tmp) / "repo" / "tests" / "t.py").exists())


if __name__ == "__main__":
    unittest.main()
