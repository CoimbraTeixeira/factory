#!/usr/bin/env python3
"""Refresh the recordings the fake services replay.

The fakes are only as honest as what they serve, so the recordings are real responses
from the real services, captured here rather than written by hand: a hand-written feed
or index holds the cases someone thought of, and the bugs these monitors have had came
from the cases nobody thought of (a scanned paper filing, a publish gap, a feed that
mixes commentary into its deals).

This is the one part of the factory that touches the network, and it only reads. It
never runs a monitor, never writes into another repo, and never reads a config holding
a webhook.

  ./fakes/record.py all                                    # the committed initial set
  ./fakes/record.py clerk --years 2026 --doc-ids 20035553  # one more filing
  ./fakes/record.py sources                                # every fare-monitor source
  ./fakes/record.py yahoo --tickers KO MSFT                # value-screener cache files
  ./fakes/record.py congress                               # leaderboard + watchlist

Layout under --recordings (default fakes/recordings), mirroring the URL each fake serves:

  clerk/public_disc/financial-pdfs/<year>FD.zip
  clerk/public_disc/ptr-pdfs/<year>/<doc_id>.pdf
  sources/<slug>.<xml|html>, sources/sources.json   (FARE_MONITOR_SOURCES, {base} URLs)
  yahoo/<TICKER>.<quote|statements|forward>.json      (value-screener cache format)
  congress/leaderboard.json, congress/watchlist.json
"""

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

FAKES_DIR = Path(__file__).resolve().parent
PRE_COMMIT_HOOK = FAKES_DIR.parent / "hooks" / "pre-commit"
DEFAULT_RECORDINGS = FAKES_DIR / "recordings"
DEFAULT_REPOS_DIR = Path.home() / "Documents" / "repos"

CLERK_BASE_URL = "https://disclosures-clerk.house.gov"
# The monitor's own User-Agent, so the Clerk serves the recorder what it serves the cron job.
CLERK_HEADERS = {"User-Agent": "congress-trade-monitor/1.0"}
INDEX_PATH_TMPL = "public_disc/financial-pdfs/{year}FD.zip"
PDF_PATH_TMPL = "public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

# The initial set, chosen for the cases that have bitten before: an electronic filing
# that parses into transactions, and a 31-page scanned paper filing that has no text
# layer at all (the case once counted as 247 failures).
INITIAL_YEARS = [2026]
INITIAL_DOC_IDS = ["20035553", "9116376"]
INITIAL_TICKERS = ["KO", "MSFT", "OTEX", "NOVO-B.CO"]

YAHOO_KINDS = ("quote", "statements", "forward")
SOURCES_BASE = "{base}"
REDACTED = "REDACTED-BY-RECORDER"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DOC_ID_RE = re.compile(r"^\d{1,12}$")


def slug(name: str) -> str:
    """'Travel Dealz DE' -> 'travel-dealz-de': a stable filename for a source name."""
    return _SLUG_RE.sub("-", name.lower()).strip("-")


def load_secret_patterns() -> list:
    """The secret scan's own patterns, read from hooks/pre-commit, so the recorder
    redacts exactly what CI would block -- no second list to drift out of step."""
    loader = importlib.machinery.SourceFileLoader("factory_pre_commit", str(PRE_COMMIT_HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module.PATTERNS


def redact(data: bytes, patterns: list) -> tuple[bytes, int]:
    """Blank out credential-shaped strings in a text recording.

    Third-party pages carry their own publishable keys (Travel Dealz embeds a Google
    Maps browser key in every post). They are not ours to republish, they mean nothing
    to any monitor, and committing them would trip the secret scan -- which must stay
    strict rather than learn exceptions. Binary files are left alone, as the scan
    skips them too. Unchanged input is returned byte for byte."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, 0
    total = 0
    for _label, pattern in patterns:
        text, count = pattern.subn(REDACTED, text)
        total += count
    return (text.encode("utf-8"), total) if total else (data, 0)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace a recording whole, so a failed or interrupted refresh never leaves a
    truncated file for the fakes to serve as if the real service had sent it.
    Credential-shaped strings are redacted on the way in (see redact)."""
    data, redactions = redact(data, load_secret_patterns())
    if redactions:
        print(f"  redacted {redactions} credential-shaped string(s) in {path.name}",
              file=sys.stderr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
        handle.write(data)
        tmp = handle.name
    os.replace(tmp, path)


def http_get(url: str, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def index_doc_years(index_zip: Path) -> dict[str, str]:
    """DocID -> filing year, read from a recorded index archive."""
    with zipfile.ZipFile(index_zip) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".txt"))
        lines = archive.read(name).decode("utf-8", errors="replace").splitlines()
    header = lines[0].split("\t")
    years = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) == len(header):
            row = dict(zip(header, fields))
            years[row["DocID"]] = row["Year"]
    return years


def record_clerk(recordings: Path, years: list[int], doc_ids: list[str],
                 base_url: str = CLERK_BASE_URL) -> list[Path]:
    """Index archives for `years`, then the PTR PDFs for `doc_ids`.

    A PDF's URL needs its filing year, and the index already says which year each DocID
    was filed in, so the caller names only the DocID."""
    clerk = recordings / "clerk"
    written = []
    for year in years:
        path = clerk / INDEX_PATH_TMPL.format(year=year)
        write_atomic(path, http_get(f"{base_url}/{INDEX_PATH_TMPL.format(year=year)}",
                                    CLERK_HEADERS))
        written.append(path)

    doc_years: dict[str, str] = {}
    for index_zip in sorted((clerk / "public_disc" / "financial-pdfs").glob("*FD.zip")):
        doc_years.update(index_doc_years(index_zip))
    for doc_id in doc_ids:
        if not _DOC_ID_RE.match(doc_id):
            raise SystemExit(f"Not a DocID: {doc_id!r}")
        year = doc_years.get(doc_id)
        if year is None:
            raise SystemExit(f"DocID {doc_id} is in no recorded index; record its year "
                             f"with --years first.")
        rel = PDF_PATH_TMPL.format(year=year, doc_id=doc_id)
        path = clerk / rel
        write_atomic(path, http_get(f"{base_url}/{rel}", CLERK_HEADERS))
        written.append(path)
    return written


def load_fare_monitor(path: Path):
    """Import fare_monitor.py from its checkout to read FEEDS and PAGES.

    Reading the lists from the monitor itself keeps the recordings in step with the
    sources production actually polls. FARE_MONITOR_SOURCES is cleared first: set, it
    would hand back the fakes' own URLs and the recorder would record itself."""
    os.environ.pop("FARE_MONITOR_SOURCES", None)
    spec = importlib.util.spec_from_file_location("fare_monitor_for_recording", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sources_manifest(feeds: dict[str, str], pages: dict[str, str]) -> dict:
    """The FARE_MONITOR_SOURCES document for the recorded sources, with a `{base}`
    placeholder where the fake server's address goes (the sandbox fills it in)."""
    return {
        "feeds": {name: f"{SOURCES_BASE}/{slug(name)}.xml" for name in feeds},
        "pages": {name: f"{SOURCES_BASE}/{slug(name)}.html" for name in pages},
    }


def record_sources(recordings: Path, fare_monitor_path: Path) -> tuple[list[Path], list[str]]:
    """Every feed and page fare-monitor polls, fetched with its own http_get (same
    User-Agent, same retries), so a site that serves bots differently is recorded as
    the monitor sees it. A source that fails keeps its previous recording."""
    monitor = load_fare_monitor(fare_monitor_path)
    sources_dir = recordings / "sources"
    written, failed = [], []
    targets = [(name, url, "xml") for name, url in monitor.FEEDS.items()]
    targets += [(name, url, "html") for name, url in monitor.PAGES.items()]
    for name, url, ext in targets:
        try:
            body = monitor.http_get(url).content
        except Exception as exc:  # any failure: report it, keep the old file
            failed.append(f"{name}: {exc}")
            continue
        path = sources_dir / f"{slug(name)}.{ext}"
        write_atomic(path, body)
        written.append(path)
    manifest = sources_dir / "sources.json"
    write_atomic(manifest, (json.dumps(sources_manifest(monitor.FEEDS, monitor.PAGES),
                                       indent=2) + "\n").encode())
    written.append(manifest)
    return written, failed


def yahoo_cache_name(ticker: str, kind: str) -> str:
    """value-screener's market_data._cache_path naming, so the files drop straight into
    a VALUE_SCREENER_DATA_DIR's cache/."""
    return f"{ticker.upper().replace('/', '_')}.{kind}.json"


def record_yahoo(recordings: Path, cache_dir: Path,
                 tickers: list[str]) -> tuple[list[Path], list[str]]:
    """Copy value-screener's cached Yahoo responses. The screener already stores what
    Yahoo returned, in the format its offline mode reads, so the cache *is* the
    recording; copying it costs no Yahoo requests. Read-only on the source dir."""
    yahoo = recordings / "yahoo"
    written, missing = [], []
    for ticker in tickers:
        for kind in YAHOO_KINDS:
            source = cache_dir / yahoo_cache_name(ticker, kind)
            if not source.is_file():
                missing.append(str(source))
                continue
            json.loads(source.read_text())  # refuse to record a corrupt cache entry
            path = yahoo / source.name
            write_atomic(path, source.read_bytes())
            written.append(path)
    return written, missing


def trimmed_leaderboard(leaderboard: dict) -> dict:
    """The leaderboard without each member's per-trade detail.

    The monitors read members' identity and ranking figures; the per-trade list feeds
    only the dashboard and the search index, and is 94% of the file's 1.9 MB. It stays
    as an empty list so anything iterating it still works."""
    trimmed = dict(leaderboard)
    trimmed["members"] = [dict(m, trade_detail=[]) for m in leaderboard.get("members", [])]
    return trimmed


def record_congress(recordings: Path, congress_dir: Path) -> list[Path]:
    """The leaderboard and watchlist the congress-trades monitors read. They are built
    from a three-year rebuild, not fetched, so the recording is today's build output."""
    out = recordings / "congress"
    leaderboard = json.loads((congress_dir / "leaderboard.json").read_text())
    watchlist = json.loads((congress_dir / "watchlist.json").read_text())
    paths = [out / "leaderboard.json", out / "watchlist.json"]
    write_atomic(paths[0], (json.dumps(trimmed_leaderboard(leaderboard), indent=1)
                            + "\n").encode())
    write_atomic(paths[1], (json.dumps(watchlist, indent=1) + "\n").encode())
    return paths


def report(paths: list[Path], recordings: Path) -> None:
    for path in paths:
        print(f"  {path.relative_to(recordings)}  {path.stat().st_size:,} bytes")


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS)
    common.add_argument("--repos-dir", type=Path, default=DEFAULT_REPOS_DIR,
                        help="Where the three monitor checkouts live (read only)")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="what", required=True)
    clerk = sub.add_parser("clerk", parents=[common],
                           help="House Clerk index archives and PTR PDFs")
    clerk.add_argument("--years", type=int, nargs="+", default=INITIAL_YEARS)
    clerk.add_argument("--doc-ids", nargs="*", default=INITIAL_DOC_IDS)
    sub.add_parser("sources", parents=[common], help="Every fare-monitor feed and page")
    yahoo = sub.add_parser("yahoo", parents=[common],
                           help="value-screener's cached Yahoo responses")
    yahoo.add_argument("--cache-dir", type=Path,
                       help="Default: <repos-dir>/value-screener/cache")
    yahoo.add_argument("--tickers", nargs="+", default=INITIAL_TICKERS)
    sub.add_parser("congress", parents=[common],
                   help="congress-trades leaderboard.json and watchlist.json")
    sub.add_parser("all", parents=[common],
                   help="The initial set: everything above with its defaults")
    args = parser.parse_args()

    recordings = args.recordings.resolve()
    what = args.what
    problems: list[str] = []
    if what in ("clerk", "all"):
        years = args.years if what == "clerk" else INITIAL_YEARS
        doc_ids = args.doc_ids if what == "clerk" else INITIAL_DOC_IDS
        print("House Clerk:")
        report(record_clerk(recordings, years, doc_ids), recordings)
    if what in ("sources", "all"):
        print("fare-monitor sources:")
        written, failed = record_sources(recordings,
                                         args.repos_dir / "fare-monitor" / "fare_monitor.py")
        report(written, recordings)
        problems += [f"source failed, previous recording kept: {f}" for f in failed]
    if what in ("yahoo", "all"):
        cache_dir = getattr(args, "cache_dir", None) or args.repos_dir / "value-screener" / "cache"
        tickers = args.tickers if what == "yahoo" else INITIAL_TICKERS
        print("Yahoo (value-screener cache):")
        written, missing = record_yahoo(recordings, cache_dir, tickers)
        report(written, recordings)
        problems += [f"no cache file: {m}" for m in missing]
    if what in ("congress", "all"):
        print("congress-trades build output:")
        report(record_congress(recordings, args.repos_dir / "congress-trades"), recordings)

    total = sum(p.stat().st_size for p in recordings.rglob("*") if p.is_file())
    print(f"Recordings total: {total:,} bytes")
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
