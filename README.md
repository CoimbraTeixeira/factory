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

## Tests

```sh
python3 -m unittest discover -s tests
```

No network: rule thresholds and windows, crash grouping (chained tracebacks, volatile
numbers, network-vs-defect thresholds), last-run scoping for the untimestamped rebuild
log, dead cron targets, and the issue lifecycle with `gh` stubbed.
