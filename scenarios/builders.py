"""Synthetic inputs for scenarios: feeds, pages, recorded quotes, filings.

Recorded snapshots go stale -- fare-monitor only alerts on deals from the last 24 hours,
so a feed recorded yesterday has nothing left to find. These build inputs dated relative
to the moment a scenario runs, in the exact shapes the monitors parse, and write them
into a scenario's private copy of the recordings.
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path


def now() -> datetime:
    return datetime.now(timezone.utc)


def rss_feed(items: list[dict]) -> str:
    """An RSS 2.0 document. Each item: title, link, and optionally description,
    categories, guid, and age_hours (how long ago it was published; default 1)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>',
             "<title>Synthetic feed</title>"]
    for item in items:
        published = now() - timedelta(hours=item.get("age_hours", 1))
        parts.append("<item>")
        parts.append(f"<title>{escape(item['title'])}</title>")
        parts.append(f"<link>{escape(item['link'])}</link>")
        parts.append(f"<guid>{escape(item.get('guid', item['link']))}</guid>")
        parts.append(f"<pubDate>{format_datetime(published)}</pubDate>")
        parts.append(f"<description>{escape(item.get('description', ''))}</description>")
        parts += [f"<category>{escape(c)}</category>" for c in item.get("categories", ())]
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts)


def secret_flying_page(posts: list[dict]) -> str:
    """A listing page in Secret Flying's markup. Each post: slug, title, and optionally
    age_hours and expired (adds the EXPIRED stamp image the site puts on pulled deals).
    Links are absolute secretflying.com URLs, as on the live page."""
    blocks = []
    for post in posts:
        url = f"https://www.secretflying.com/posts/{post['slug']}/"
        published = (now() - timedelta(hours=post.get("age_hours", 1))).isoformat()
        stamp = ('<img src="https://www.secretflying.com/wp-content/uploads/2014/10/'
                 'EXPIRED1.gif" class="expiredStamp">' if post.get("expired") else "")
        title = escape(post["title"])
        blocks.append(
            f'<article class="article-content-wrapper hentry">'
            f'<div class="entry-img">{stamp}<a href="{url}" title="{title}"></a></div>'
            f'<div class="entry-block"><h3 class="entry-title"><a href="{url}">{title}</a></h3>'
            f'<time class="entry-date" datetime="{published}">today</time></div></article>')
    return "<html><body>" + "\n".join(blocks) + "</body></html>"


def write_sources(recordings: Path, feeds: dict[str, str] | None = None,
                  pages: dict[str, str] | None = None) -> None:
    """Replace the recorded source list with exactly these sources. Values are
    {name: (filename, content)}; files land in recordings/sources/ and the manifest
    points at them through the fake server, so no other source is read."""
    sources = recordings / "sources"
    if sources.exists():
        shutil.rmtree(sources)
    sources.mkdir(parents=True)
    manifest = {"feeds": {}, "pages": {}}
    for kind, entries in (("feeds", feeds or {}), ("pages", pages or {})):
        for name, (filename, content) in entries.items():
            (sources / filename).write_text(content)
            manifest[kind][name] = "{base}/" + filename
    (sources / "sources.json").write_text(json.dumps(manifest, indent=2))


def write_quote(recordings: Path, ticker: str, *, price: float, dividend_rate: float | None,
                eps: float = 8.0, book: float = 40.0, fcf: float = 4.5e8,
                fcf_history: tuple = (3.8e8, 4.5e8), historical_pe: tuple = (11.0, 11.0),
                shares: float = 1e8, revisions_down: int = 0, name: str | None = None) -> None:
    """The three recorded Yahoo files for one ticker. Defaults describe a healthy
    business whose valuation models agree, so price alone decides BUY vs WATCH."""
    yahoo = recordings / "yahoo"
    yahoo.mkdir(parents=True, exist_ok=True)
    quote = {
        "currentPrice": price, "regularMarketPrice": price, "previousClose": price,
        "shortName": name or f"{ticker} Corp", "longName": name or f"{ticker} Corporation",
        "currency": "USD", "quoteType": "EQUITY", "sector": "Industrials",
        "longBusinessSummary": f"{ticker} makes things.", "trailingEps": eps,
        "bookValue": book, "freeCashflow": fcf, "sharesOutstanding": shares,
        "totalCash": 2e8, "totalDebt": 1e8, "beta": 1.0, "dividendRate": dividend_rate,
        "payoutRatio": 0.4, "returnOnEquity": 0.18, "debtToEquity": 60.0,
        "earningsGrowth": 0.05, "targetMeanPrice": None, "targetHighPrice": None,
        "targetLowPrice": None, "fetched_at": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    statements = {"fcf_history": list(fcf_history), "eps_history": [eps] * len(fcf_history),
                  "historical_pe": list(historical_pe)}
    forward = {"eps_next_year": None, "analyst_count": 0, "revisions_up": 0,
               "revisions_down": revisions_down, "eps_drift_90d": None,
               "days_to_earnings": None, "insider_net_shares_6m": None, "share_growth": None}
    for kind, payload in (("quote", quote), ("statements", statements), ("forward", forward)):
        (yahoo / f"{ticker}.{kind}.json").write_text(json.dumps(payload))


def place_pdf(recordings: Path, year: int | str, doc_id: str, source: Path) -> None:
    """Publish a filing's PDF on the fake Clerk. Done between steps, it is the moment
    the real Clerk makes a document reachable after already indexing it."""
    target = recordings / "clerk" / "public_disc" / "ptr-pdfs" / str(year) / f"{doc_id}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def remove_pdf(recordings: Path, year: int | str, doc_id: str) -> None:
    """Withhold a filing's PDF: the fake Clerk answers 404, the publish gap."""
    (recordings / "clerk" / "public_disc" / "ptr-pdfs" / str(year) / f"{doc_id}.pdf").unlink(
        missing_ok=True)
