# factory

Infrastructure for running congress-trades, fare-monitor and value-screener as a
software factory: agents do the work, automated gates decide what can merge, and
production problems arrive as tasks instead of being found by reading logs.

Step 1, gates on every repo, lives in each repo (`.github/workflows/ci.yml`, protected
`master`). This repo holds what spans all of them.

## Log auditor

`log_auditor.py` reads the cron logs nightly and files what it finds as GitHub issues
on the repo that owns the log, labelled `log-auditor`. Those issues are the factory's
work queue.

```sh
./log_auditor.py --dry-run     # print findings; file, post and record nothing
./log_auditor.py               # file/reopen issues, post machine-level findings
```

```cron
15 6 * * * /usr/bin/python3 /home/rogerio/Documents/repos/factory/log_auditor.py >> /home/rogerio/Documents/repos/factory/auditor.log 2>&1
```

It exists because every production bug fixed so far was found by a person reading a log,
with the unit tests passing throughout: a filing "awaiting a PDF" across 88 hourly runs,
247 scanned filings counted as failures, two cron jobs pointing at a deleted directory.

### What it checks

| Check | Fires when |
|---|---|
| `log-silent` / `log-missing` | A log has not been written for longer than its job's schedule allows (3h for the hourly monitors, 30h for the daily jobs, 120h for the weekday-only market-open post, which spans long weekends) |
| `crash` | A run ended in an uncaught exception in the last 24h. Network failures (DNS, timeouts, connection resets) are someone else's outage, so they need 6 in a day |
| `pdf-wait-recurring` | Filings sat "awaiting a PDF" 30+ times in a week: the publish gap is longer than expected, or a kind of filing is being misread as unpublished |
| `zero-extraction`, `source-failing` | A deal source parsed to nothing 3 times, or failed to fetch 6 times, in a day; one issue per source |
| `discord-delivery` | Any alert that Discord did not accept |
| `alert-backlog` | `--max-alerts` truncated 3 runs in a day |
| `ptr-failures`, `benchmark-unpriced` | The last leaderboard rebuild had more than 10 real PTR failures, or SPY did not price |
| `no-results` | The market-open screen fetched nothing and skipped its post |
| `cron-dead-target` | A crontab entry runs a script that no longer exists |

The rules are data (`LOGS` at the top of the script); adding a check for a new failure
mode is a few lines.

### Issue lifecycle

- A finding is identified by a fingerprint (repo, check, key) that ignores counts and
  timestamps, so the same problem maps to the same issue every night.
- An **open** issue is left alone.
- **Closing** an issue means "fixed as of now". The finding reopens it only on evidence
  newer than the close, so the log lines that prompted a fix cannot reopen it while
  they age out of the window.
- Findings no repo owns (`cron-dead-target`) go to the Discord webhook in
  `~/.config/fare-monitor/config.json`, once each; one that clears and later returns is
  posted again.

State is in `state/log_auditor.json` (gitignored).

## Fake services

`fakes/` lets each monitor run end to end -- real HTTP, real parsing, real posting --
against local stand-ins for everything outside the machine, so a scenario test can run
the production code with no real side effects. Unit tests stub the boundary with the
Clerk, the deal sites and Discord, and that boundary is where the worst bugs lived: a
PDF indexed before it was published, a 2001-character message, a 429 mid-alert.

### What the fakes serve

`fakes/fake_services.py` is one stdlib HTTP server on loopback, replaying real responses
recorded in `fakes/recordings/`:

| Route | Serves | Stands in for |
|---|---|---|
| `GET /clerk/<path>` | `recordings/clerk/<path>` | disclosures-clerk.house.gov (index ZIPs, PTR PDFs). A file that is not there is a 404, which is the Clerk's publish gap: delete a PDF to stage it |
| `GET /sources/<file>` | `recordings/sources/<file>` | the fare-monitor feeds and pages, with a UTF-8 content type |
| `POST /api/webhooks/<id>/<token>` | appends `{received_at, path, content}` to the `--posts` JSONL file, 204 | Discord: content over 2000 characters is a 400 `Invalid Form Body` (50035), `--rate-limit-first N` answers the first N posts with 429 `{"retry_after": 0.01}` |

Yahoo has no server: value-screener's offline mode reads its own cache files, so the
recorded cache JSON in `recordings/yahoo/` is the fake. Paths are resolved before they
are served, so nothing outside `recordings/` is reachable.

```sh
./fakes/fake_services.py --port 0 --posts /tmp/posts.jsonl   # prints the port it picked
```

### Pointing a monitor at them

Every seam is an environment variable; unset, each repo behaves exactly as in production.

| Repo | Variable | Sandbox value |
|---|---|---|
| all three | `MONITOR_CONFIG` | a config whose `webhook_url` is `http://127.0.0.1:<port>/api/webhooks/<id>/<token>` (the repos accept a loopback webhook besides Discord's) |
| congress-trades | `HOUSE_CLERK_BASE_URL` | `http://127.0.0.1:<port>/clerk` |
| congress-trades | `CONGRESS_TRADES_DATA_DIR` | a temp dir for `state/`, `cache/`, `leaderboard.json`, `watchlist.json` and the other build outputs |
| fare-monitor | `FARE_MONITOR_STATE_DIR` | a temp dir for `seen.json` |
| fare-monitor | `FARE_MONITOR_SOURCES` | `recordings/sources/sources.json` with `{base}` replaced by `http://127.0.0.1:<port>/sources` |
| value-screener | `VALUE_SCREENER_DATA_DIR` | a temp dir holding `cache/` (the Yahoo recordings) and a `watchlist.json` of the recorded tickers |
| value-screener | `VALUE_SCREENER_OFFLINE` | `1`: read the cache only, never Yahoo |

`fakes/sandbox.py` does all of that: a temp dir, the server on a free port, the env for a
repo, a run, and the posts it made. It also moves `HOME` into the sandbox, so a path a
seam missed cannot reach the real `~/.config/fare-monitor/config.json`, and it refuses a
checkout whose code does not read every seam (a pre-seam checkout would write its state
into its own working tree).

```python
from sandbox import Sandbox
with Sandbox() as box:
    box.seed_congress_state()   # stage the recorded PDFs' filings as just published
    result = box.run("congress-trades", ["congress_monitor.py"], repos_dir)
    result.returncode, result.stdout, result.posts
```

### Smoke run

Each monitor once, against one sandbox, printing what each posted: `congress_monitor.py`,
`watchlist_monitor.py` and `daily_summary.py` with the recorded leaderboard and watchlist
and the two recorded filings staged as new, `fare_monitor.py`, and
`market_open_monitor.py --force --now <a Tuesday open>`.

```sh
python3 fakes/sandbox.py smoke --repos-dir /home/rogerio/Documents/repos
python3 fakes/sandbox.py run --seed-congress congress-trades congress_monitor.py --verbose
```

Running from the live checkouts is safe once their seams are merged: everything they
write goes to the sandbox, which is deleted afterwards (`--keep` leaves it).

### Refreshing the recordings

```sh
./fakes/record.py all        # the committed set, below
./fakes/record.py clerk --years 2026 --doc-ids 20035553 9116376
./fakes/record.py sources    # every FEEDS/PAGES entry, read from fare_monitor.py itself
./fakes/record.py yahoo --tickers KO MSFT OTEX NOVO-B.CO   # from value-screener/cache
./fakes/record.py congress   # leaderboard.json (minus per-trade detail) and watchlist.json
```

The only network use in the factory, and read-only: it never runs a monitor or writes
into another repo. A source that fails keeps its previous recording. Anything the secret
scan would block is redacted on the way in -- Travel Dealz embeds its own Google Maps
browser key in every post, which is not ours to republish.

The committed set: the 2026 Clerk index; PTR 20035553 (electronic) and 9116376 (a
31-page scanned paper filing); every fare-monitor source; Yahoo cache files for KO, MSFT,
OTEX and NOVO-B.CO; the congress leaderboard and watchlist. About 3.3 MB, most of it the
two Travel Dealz feeds and the scanned PDF. The feeds are a snapshot, so fare-monitor's
24-hour lookback finds nothing in them a day after recording; a scenario that needs a
fresh deal writes its own feed into a copy of the recordings.

## Tests

```sh
python3 -m unittest discover -s tests
```

No network: rule thresholds and windows, crash grouping (chained tracebacks, volatile
numbers, network-vs-defect thresholds), last-run scoping for the untimestamped rebuild
log, dead cron targets, and the issue lifecycle with `gh` stubbed. For the fakes: routing,
the 404 publish gap, path traversal, the webhook's 204/400/429, the recorder with its
fetches stubbed, and the sandbox's env and runs against stand-in checkouts -- none of it
needs the monitor repos.

## CI and merging

Same gates as the repos it serves: `master` is protected, a change lands only through a
pull request once the `check` job (`.github/workflows/ci.yml`) passes, and that applies
to admins too. `check` runs the secret scan over every tracked file
(`python3 hooks/pre-commit --all`), `ruff check .` (syntax errors + pyflakes, rules in
`ruff.toml`) and the tests. Enable the local copy of the secret guard once per clone with
`git config core.hooksPath hooks`.
