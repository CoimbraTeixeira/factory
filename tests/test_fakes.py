"""Tests for the fake services, the recorder and the sandbox.

Run with: python3 -m unittest discover -s tests

No network and no monitor checkouts: the server is exercised over loopback, the
recorder with its fetches stubbed, and the sandbox with a stand-in monitor script.
"""

import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

FAKES_DIR = Path(__file__).resolve().parent.parent / "fakes"
sys.path.insert(0, str(FAKES_DIR))

import fake_services as fs  # noqa: E402
import record  # noqa: E402
import sandbox  # noqa: E402

INDEX_HEADER = "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID"


def index_zip(rows: list[str]) -> bytes:
    """A Clerk index archive: a tab-separated .txt (plus the .xml the real one carries)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("2026FD.txt", "\n".join([INDEX_HEADER, *rows]) + "\n")
        archive.writestr("2026FD.xml", "<FinancialDisclosure/>")
    return buffer.getvalue()


ROWS = [
    "Hon.\tPelosi\tNancy\t\tP\tCA11\t2026\t10/2/2026\t20035553",
    "Hon.\tKhanna\tRohit\t\tP\tCA17\t2026\t10/7/2026\t9116376",
    "Hon.\tEvans\tDwight\t\tP\tPA03\t2026\t9/1/2026\t20030000",
    "Hon.\tEvans\tDwight\t\tO\tPA03\t2026\t5/1/2026\t10060000",
]


def make_recordings(root: Path) -> Path:
    recordings = root / "recordings"
    clerk = recordings / "clerk" / "public_disc"
    (clerk / "financial-pdfs").mkdir(parents=True)
    (clerk / "financial-pdfs" / "2026FD.zip").write_bytes(index_zip(ROWS))
    (clerk / "ptr-pdfs" / "2026").mkdir(parents=True)
    (clerk / "ptr-pdfs" / "2026" / "20035553.pdf").write_bytes(b"%PDF-1.4 fake")
    sources = recordings / "sources"
    sources.mkdir()
    (sources / "deals.xml").write_text("<rss><title>Café error fare</title></rss>",
                                       encoding="utf-8")
    (sources / "sources.json").write_text(json.dumps(
        {"feeds": {"Deals": "{base}/deals.xml"}, "pages": {}}))
    yahoo = recordings / "yahoo"
    yahoo.mkdir()
    for kind in record.YAHOO_KINDS:
        (yahoo / f"NOVO-B.CO.{kind}.json").write_text("{}")
    (yahoo / "KO.quote.json").write_text("{}")
    congress = recordings / "congress"
    congress.mkdir()
    (congress / "leaderboard.json").write_text(json.dumps({"members": [
        {"member_key": ["evans", "PA03"]}, {"member_key": ["pelosi", "CA11"]}]}))
    (congress / "watchlist.json").write_text(json.dumps({"members": [
        {"member_key": ["pelosi", "CA11"]}, {"member_key": ["greene", "GA14"]}]}))
    (root / "secret.txt").write_text("outside the recordings")
    return recordings


class FakeServerCase(unittest.TestCase):
    rate_limit_first = 0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.recordings = make_recordings(self.root)
        self.posts_path = self.root / "posts.jsonl"
        self.fakes = fs.FakeServices(self.recordings, self.posts_path,
                                     rate_limit_first=self.rate_limit_first).start()

    def tearDown(self):
        self.fakes.stop()
        self.tmp.cleanup()

    def get(self, raw_path: str) -> tuple[int, dict, bytes]:
        """GET with the path sent exactly as given -- urllib would normalise `..`."""
        conn = http.client.HTTPConnection("127.0.0.1", self.fakes.port, timeout=5)
        try:
            conn.request("GET", raw_path)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def post(self, content, url: str | None = None) -> tuple[int, dict]:
        body = json.dumps({"content": content}).encode()
        request = urllib.request.Request(url or self.fakes.webhook_url, data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, {}
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class RoutingTest(FakeServerCase):
    def test_clerk_route_serves_the_recorded_file(self):
        status, headers, body = self.get("/clerk/public_disc/financial-pdfs/2026FD.zip")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertEqual(body, index_zip(ROWS))

    def test_clerk_base_url_matches_the_monitor_url_template(self):
        # congress-trades builds base + /public_disc/ptr-pdfs/{year}/{doc_id}.pdf
        url = f"{self.fakes.clerk_base_url}/public_disc/ptr-pdfs/2026/20035553.pdf"
        with urllib.request.urlopen(url, timeout=5) as resp:
            self.assertEqual(resp.headers["Content-Type"], "application/pdf")
            self.assertEqual(resp.read(), b"%PDF-1.4 fake")

    def test_unpublished_pdf_is_a_404_like_the_clerks_publish_gap(self):
        status, _, _ = self.get("/clerk/public_disc/ptr-pdfs/2026/9116376.pdf")
        self.assertEqual(status, 404)

    def test_sources_declare_utf8_so_titles_do_not_mojibake(self):
        status, headers, body = self.get("/sources/deals.xml")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/xml; charset=utf-8")
        self.assertIn("Café", body.decode("utf-8"))

    def test_unknown_route_and_directory_are_404(self):
        self.assertEqual(self.get("/elsewhere")[0], 404)
        self.assertEqual(self.get("/clerk/public_disc")[0], 404)

    def test_path_traversal_never_leaves_the_recordings(self):
        (self.recordings / "sources" / "link.txt").symlink_to(self.root / "secret.txt")
        for raw in ("/clerk/../secret.txt", "/sources/../../secret.txt",
                    "/clerk/%2e%2e/%2e%2e/secret.txt", "/sources/..%2f..%2fsecret.txt",
                    f"/clerk/{self.root}/secret.txt", "/sources//etc/passwd",
                    "/sources/link.txt", "/clerk/%00"):
            with self.subTest(raw=raw):
                status, _, body = self.get(raw)
                self.assertEqual(status, 404)
                self.assertNotIn(b"outside the recordings", body)


class WebhookTest(FakeServerCase):
    def test_accepted_post_is_204_and_lands_in_the_posts_file(self):
        self.assertEqual(self.post("hello")[0], 204)
        self.assertEqual(self.post("é" * 2000)[0], 204)  # the limit is characters
        posts = fs.read_posts(self.posts_path)
        self.assertEqual([p["content"] for p in posts], ["hello", "é" * 2000])
        self.assertEqual(posts[0]["path"], fs.FAKE_WEBHOOK_PATH)
        self.assertIn("received_at", posts[0])
        self.assertEqual(self.fakes.posts(), posts)

    def test_any_webhook_id_and_token_is_accepted(self):
        self.assertEqual(self.post("x", f"{self.fakes.base_url}/api/webhooks/1/abc")[0], 204)

    def test_over_limit_is_400_invalid_form_body_and_not_recorded(self):
        status, body = self.post("x" * 2001)
        self.assertEqual(status, 400)
        self.assertEqual(body, {"message": "Invalid Form Body", "code": 50035})
        self.assertEqual(fs.read_posts(self.posts_path), [])

    def test_empty_message_is_400(self):
        self.assertEqual(self.post("")[0], 400)

    def test_wrong_path_is_404(self):
        self.assertEqual(self.post("x", f"{self.fakes.base_url}/api/webhooks/1")[0], 404)

    def test_no_posts_file_reads_as_no_posts(self):
        self.assertEqual(fs.read_posts(self.root / "absent.jsonl"), [])


class RateLimitTest(FakeServerCase):
    rate_limit_first = 2

    def test_first_n_posts_get_429_with_retry_after(self):
        first, second = self.post("one"), self.post("two")
        for status, body in (first, second):
            self.assertEqual(status, 429)
            self.assertEqual(body["retry_after"], 0.01)
        self.assertEqual(self.post("three")[0], 204)
        self.assertEqual([p["content"] for p in fs.read_posts(self.posts_path)], ["three"])


class ServerCliTest(unittest.TestCase):
    def test_port_zero_picks_a_free_port_and_prints_it_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            recordings = make_recordings(Path(tmp))
            proc = subprocess.Popen(
                [sys.executable, str(FAKES_DIR / "fake_services.py"), "--port", "0",
                 "--recordings", str(recordings), "--posts", str(Path(tmp) / "p.jsonl")],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                port = int(proc.stdout.readline())
                url = f"http://127.0.0.1:{port}/sources/deals.xml"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdout.close()


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_slug(self):
        self.assertEqual(record.slug("Travel Dealz DE"), "travel-dealz-de")
        self.assertEqual(record.slug("Fly4Free"), "fly4free")

    def test_redact_blanks_what_the_secret_scan_would_block(self):
        key = "AIza" + "B" * 35  # assembled so this file itself stays clean
        data = f'<iframe src="maps/embed?key={key}&amp;q=x">'.encode()
        redacted, count = record.redact(data, record.load_secret_patterns())
        self.assertEqual(count, 1)
        self.assertNotIn(key.encode(), redacted)
        self.assertIn(record.REDACTED.encode(), redacted)

    def test_redact_leaves_clean_and_binary_data_byte_for_byte(self):
        patterns = record.load_secret_patterns()
        for data in (b"plain\r\ntext", b"\xff\xfe\x00binary"):
            self.assertEqual(record.redact(data, patterns), (data, 0))

    def test_sources_manifest_uses_the_base_placeholder(self):
        manifest = record.sources_manifest({"The Flight Deal": "https://a/feed/"},
                                           {"Secret Flying": "https://b/"})
        self.assertEqual(manifest, {
            "feeds": {"The Flight Deal": "{base}/the-flight-deal.xml"},
            "pages": {"Secret Flying": "{base}/secret-flying.html"},
        })

    def test_record_clerk_finds_each_pdfs_year_in_the_index(self):
        fetched = []

        def fake_get(url, headers):
            fetched.append(url)
            return index_zip(ROWS) if url.endswith("FD.zip") else b"%PDF"

        with mock.patch.object(record, "http_get", fake_get):
            record.record_clerk(self.root, [2026], ["9116376"], base_url="https://clerk")
            with self.assertRaises(SystemExit):
                record.record_clerk(self.root, [], ["12345678"], base_url="https://clerk")
        self.assertEqual(fetched, [
            "https://clerk/public_disc/financial-pdfs/2026FD.zip",
            "https://clerk/public_disc/ptr-pdfs/2026/9116376.pdf",
        ])
        self.assertTrue((self.root / "clerk/public_disc/ptr-pdfs/2026/9116376.pdf").is_file())

    def test_record_sources_reads_the_monitors_own_lists(self):
        monitor = self.root / "fare_monitor.py"
        monitor.write_text(
            "import os\n"
            "class R:\n"
            "    def __init__(self, url): self.content = url.encode()\n"
            "FEEDS = {'Deal Feed': 'https://deals/feed/'}\n"
            "PAGES = {'Some Page': 'https://page/'}\n"
            "LEAKED = os.environ.get('FARE_MONITOR_SOURCES')\n"
            "def http_get(url):\n"
            "    if 'page' in url: raise OSError('403')\n"
            "    return R(url)\n")
        recordings = self.root / "recordings"
        with mock.patch.dict(os.environ, {"FARE_MONITOR_SOURCES": "/fake/sources.json"}):
            written, failed = record.record_sources(recordings, monitor)
        self.assertEqual((recordings / "sources/deal-feed.xml").read_bytes(),
                         b"https://deals/feed/")
        self.assertEqual(len(failed), 1)
        self.assertIn("Some Page", failed[0])
        manifest = json.loads((recordings / "sources/sources.json").read_text())
        self.assertEqual(manifest["pages"], {"Some Page": "{base}/some-page.html"})
        self.assertEqual(len(written), 2)

    def test_record_yahoo_copies_cache_files_and_reports_missing(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "KO.quote.json").write_text('{"price": 1}')
        written, missing = record.record_yahoo(self.root / "rec", cache, ["ko"])
        self.assertEqual([p.name for p in written], ["KO.quote.json"])
        self.assertEqual(len(missing), 2)

    def test_trimmed_leaderboard_keeps_ranking_drops_trade_detail(self):
        trimmed = record.trimmed_leaderboard(
            {"window_years": 3, "members": [{"member_key": ["a", "B01"],
                                              "trade_detail": [1, 2]}]})
        self.assertEqual(trimmed["members"], [{"member_key": ["a", "B01"],
                                               "trade_detail": []}])
        self.assertEqual(trimmed["window_years"], 3)


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.recordings = make_recordings(Path(self.tmp.name))
        self.box = sandbox.Sandbox(self.recordings).start()
        self.addCleanup(self.box.close)

    def test_env_wires_each_repos_seams_and_nothing_else(self):
        base = {"PATH": "/usr/bin", "HOME": "/home/real",
                "FARE_MONITOR_SOURCES": "/real/sources.json",
                "HOUSE_CLERK_BASE_URL": "https://disclosures-clerk.house.gov"}
        congress = self.box.env("congress-trades", base)
        self.assertEqual(congress["HOUSE_CLERK_BASE_URL"], self.box.server.clerk_base_url)
        self.assertEqual(congress["CONGRESS_TRADES_DATA_DIR"], str(self.box.congress_data))
        self.assertNotIn("FARE_MONITOR_SOURCES", congress)  # never inherited

        fare = self.box.env("fare-monitor", base)
        self.assertEqual(fare["FARE_MONITOR_SOURCES"], str(self.box.sources_path))
        self.assertEqual(fare["FARE_MONITOR_STATE_DIR"], str(self.box.fare_state))
        self.assertNotIn("HOUSE_CLERK_BASE_URL", fare)

        screener = self.box.env("value-screener", base)
        self.assertEqual(screener["VALUE_SCREENER_DATA_DIR"], str(self.box.screener_data))
        self.assertEqual(screener["VALUE_SCREENER_OFFLINE"], "1")

        for env in (congress, fare, screener):
            self.assertEqual(env["MONITOR_CONFIG"], str(self.box.config_path))
            self.assertEqual(env["HOME"], str(self.box.home))  # real ~/.config unreachable
            self.assertEqual(env["PYTHONUSERBASE"], sandbox.USER_BASE)
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertTrue(Path(env["HOME"]).is_relative_to(self.box.root))
        with self.assertRaises(ValueError):
            self.box.env("log-auditor")

    def test_webhook_config_points_at_the_loopback_fake(self):
        url = json.loads(self.box.config_path.read_text())["webhook_url"]
        self.assertTrue(url.startswith(f"http://127.0.0.1:{self.box.server.port}/api/webhooks/"))

    def test_sources_point_at_the_fake_server(self):
        sources = json.loads(self.box.sources_path.read_text())
        self.assertEqual(sources["feeds"],
                         {"Deals": f"{self.box.server.sources_base_url}/deals.xml"})
        with urllib.request.urlopen(sources["feeds"]["Deals"], timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_yahoo_recordings_become_the_screener_cache_and_watchlist(self):
        cache = sorted(p.name for p in (self.box.screener_data / "cache").iterdir())
        self.assertIn("NOVO-B.CO.forward.json", cache)
        watchlist = json.loads((self.box.screener_data / "watchlist.json").read_text())
        self.assertEqual(watchlist, {"tickers": ["KO", "NOVO-B.CO"]})

    def test_seed_congress_state_stages_recorded_pdfs_as_new(self):
        fresh = self.box.seed_congress_state()
        self.assertEqual(fresh, {"20035553"})
        for name in ("seen_docids.json", "watchlist_seen_docids.json",
                     "summary_seen_docids.json"):
            state = json.loads((self.box.congress_data / "state" / name).read_text())
            # PTRs only (the annual report is not one), minus the fresh filing.
            self.assertEqual(state["seen"], ["20030000", "9116376"])
            self.assertEqual(state["baselined_members"],
                             [["evans", "PA03"], ["greene", "GA14"], ["pelosi", "CA11"]])

    def test_close_removes_the_sandbox(self):
        box = sandbox.Sandbox(self.recordings).start()
        box.close()
        self.assertFalse(box.root.exists())


class SandboxRunTest(unittest.TestCase):
    """run() against stand-in checkouts: the mechanics, without the real monitors."""

    FAKE_MONITOR = (
        "import json, os, pathlib, urllib.request\n"
        "# reads MONITOR_CONFIG, FARE_MONITOR_STATE_DIR, FARE_MONITOR_SOURCES\n"
        "sources = json.loads(pathlib.Path(os.environ['FARE_MONITOR_SOURCES']).read_text())\n"
        "feed = urllib.request.urlopen(sources['feeds']['Deals']).read().decode()\n"
        "url = json.loads(pathlib.Path(os.environ['MONITOR_CONFIG']).read_text())['webhook_url']\n"
        "req = urllib.request.Request(url, data=json.dumps({'content': feed}).encode(),\n"
        "                             headers={'Content-Type': 'application/json'})\n"
        "urllib.request.urlopen(req)\n"
        "print('home', os.environ['HOME'])\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repos = root / "repos"
        (self.repos / "fare-monitor").mkdir(parents=True)
        (self.repos / "fare-monitor" / "fare_monitor.py").write_text(self.FAKE_MONITOR)
        (self.repos / "congress-trades").mkdir()
        (self.repos / "congress-trades" / "congress_monitor.py").write_text(
            "# reads MONITOR_CONFIG only\nopen('/should/never/run', 'w')\n")
        self.box = sandbox.Sandbox(make_recordings(root), rate_limit_first=1).start()
        self.addCleanup(self.box.close)

    def test_run_returns_output_and_the_posts_it_made(self):
        # The stand-in does not retry, so the injected 429 makes it fail; the second
        # run gets through. Posts are per run, not cumulative.
        first = self.box.run("fare-monitor", ["fare_monitor.py"], self.repos)
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(first.posts, [])
        second = self.box.run("fare-monitor", ["fare_monitor.py"], self.repos)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual([p["content"] for p in second.posts],
                         ["<rss><title>Café error fare</title></rss>"])
        self.assertIn(str(self.box.home), second.stdout)

    def test_a_checkout_without_the_seams_is_refused_before_it_runs(self):
        self.assertEqual(sandbox.missing_seams(self.repos / "congress-trades", "congress-trades"),
                         ["HOUSE_CLERK_BASE_URL", "CONGRESS_TRADES_DATA_DIR"])
        with self.assertRaises(sandbox.SeamsMissing):
            self.box.run("congress-trades", ["congress_monitor.py"], self.repos)


class CommittedRecordingsTest(unittest.TestCase):
    """The initial recordings stay complete enough for the smoke run."""

    RECORDINGS = FAKES_DIR / "recordings"

    def test_clerk_index_holds_every_recorded_pdf(self):
        doc_ids = sandbox.index_ptr_doc_ids(self.RECORDINGS)
        pdfs = sandbox.recorded_pdf_doc_ids(self.RECORDINGS)
        self.assertEqual(pdfs, set(record.INITIAL_DOC_IDS))
        self.assertLessEqual(pdfs, doc_ids)

    def test_every_source_in_the_manifest_is_recorded(self):
        manifest = json.loads((self.RECORDINGS / "sources" / "sources.json").read_text())
        urls = [*manifest["feeds"].values(), *manifest["pages"].values()]
        self.assertTrue(urls)
        for url in urls:
            self.assertTrue(url.startswith("{base}/"), url)
            self.assertTrue((self.RECORDINGS / "sources" / url[len("{base}/"):]).is_file())

    def test_yahoo_has_all_three_cache_files_per_ticker(self):
        for ticker in record.INITIAL_TICKERS:
            for kind in record.YAHOO_KINDS:
                path = self.RECORDINGS / "yahoo" / record.yahoo_cache_name(ticker, kind)
                self.assertIsInstance(json.loads(path.read_text()), dict)


if __name__ == "__main__":
    unittest.main()
