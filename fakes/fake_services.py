#!/usr/bin/env python3
"""Local stand-ins for every outside service the monitors talk to.

The monitors' hardest bugs lived at the boundary with someone else's server: a Clerk
that indexes a filing before its PDF exists, Discord rejecting a 2001-character message,
a 429 in the middle of a multi-part alert. Unit tests stub those boundaries out, so they
cannot catch a monitor mishandling one. This server replays recorded responses from the
real services on loopback, so a monitor can run end to end -- real HTTP, real parsing,
real posting -- with nothing leaving the machine.

  GET  /clerk/<path>                    recordings/clerk/<path>  (HOUSE_CLERK_BASE_URL)
  GET  /sources/<file>                  recordings/sources/<file> (FARE_MONITOR_SOURCES)
  POST /api/webhooks/<id>/<token>       Discord webhook: 204, 400 over 2000 chars, 429

A recording that does not exist is a 404, which is exactly what the Clerk returns for a
PDF it has indexed but not yet published -- delete a PDF to stage the publish gap.

  ./fakes/fake_services.py --port 0 --posts /tmp/posts.jsonl
"""

import argparse
import json
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEFAULT_RECORDINGS = Path(__file__).resolve().parent / "recordings"
HOST = "127.0.0.1"

# Discord's hard limit; it answers a longer message with this 400 rather than
# truncating, which is the failure the monitors' message splitting exists to prevent.
DISCORD_MESSAGE_LIMIT = 2000
OVER_LIMIT_BODY = {"message": "Invalid Form Body", "code": 50035}
EMPTY_MESSAGE_BODY = {"message": "Cannot send an empty message", "code": 50006}
# Short, so a test exercising the monitors' retry loop does not sleep for real.
RATE_LIMIT_RETRY_AFTER = 0.01

WEBHOOK_RE = re.compile(r"^/api/webhooks/([^/]+)/([^/]+)$")
# The webhook the sandbox configures. Any id/token is accepted; this one just has to
# look like the real URL shape the monitors' validation expects.
FAKE_WEBHOOK_PATH = "/api/webhooks/100000000000000000/fake-token"

# Served with an explicit charset: requests decodes text/* without one as ISO-8859-1,
# which mojibakes every non-ASCII title. The real sites declare UTF-8.
CONTENT_TYPES = {
    ".zip": "application/zip",
    ".pdf": "application/pdf",
    ".xml": "application/xml; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

# URL prefix -> subdirectory of the recordings it serves.
STATIC_ROUTES = {"/clerk/": "clerk", "/sources/": "sources"}


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def safe_join(root: Path, relative: str) -> Path | None:
    """`root/relative` if it names a file inside `root`, else None.

    Resolves first and checks after, so `..`, percent-encoded `%2e%2e`, absolute
    paths and symlinks pointing out of the recordings are all caught by the one test."""
    try:
        root = root.resolve()
        candidate = (root / relative).resolve()
    except (OSError, ValueError):  # e.g. an embedded NUL byte
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


class FakeServicesHandler(BaseHTTPRequestHandler):
    server: "FakeServicesHTTPServer"

    def log_message(self, format, *args):
        if self.server.verbose:
            super().log_message(format, *args)

    def _send(self, status: int, body: bytes = b"", ctype: str | None = None,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict, headers: dict[str, str] | None = None):
        self._send(status, json.dumps(payload).encode(), "application/json", headers)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        for prefix, subdir in STATIC_ROUTES.items():
            if path.startswith(prefix):
                target = safe_join(self.server.recordings / subdir, path[len(prefix):])
                if target is None:
                    break
                self._send(200, target.read_bytes(), content_type(target))
                return
        self._send_json(404, {"message": "Not Found", "path": path})

    do_HEAD = do_GET

    def do_POST(self):
        path = urlsplit(self.path).path
        if not WEBHOOK_RE.match(path):
            self._send_json(404, {"message": "Unknown Webhook", "code": 10015})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        # Discord rate-limits before it validates, so the first N posts are refused
        # whatever they contain -- the case of a 429 landing mid-way through an alert.
        if self.server.take_rate_limit():
            self._send_json(429, {"message": "You are being rate limited.",
                                  "retry_after": RATE_LIMIT_RETRY_AFTER, "global": False},
                            {"Retry-After": str(RATE_LIMIT_RETRY_AFTER)})
            return
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            self._send_json(400, {"message": "The request body contains invalid JSON.",
                                  "code": 50109})
            return
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str) or not content:
            self._send_json(400, EMPTY_MESSAGE_BODY)
            return
        if len(content) > DISCORD_MESSAGE_LIMIT:
            self._send_json(400, OVER_LIMIT_BODY)
            return
        self.server.record_post(path, content)
        self._send(204)


class FakeServicesHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, recordings: Path, posts: Path, rate_limit_first: int = 0,
                 verbose: bool = False):
        super().__init__(address, FakeServicesHandler)
        self.recordings = Path(recordings)
        self.posts = Path(posts)
        self.verbose = verbose
        self._rate_limit_left = rate_limit_first
        self._lock = threading.Lock()

    def take_rate_limit(self) -> bool:
        with self._lock:
            if self._rate_limit_left > 0:
                self._rate_limit_left -= 1
                return True
            return False

    def record_post(self, path: str, content: str) -> None:
        """Append one accepted message to the posts file, the record of everything the
        monitor would have shown in Discord. Locked: the server is threaded and a JSONL
        line must never interleave with another."""
        line = json.dumps({"received_at": datetime.now(timezone.utc).isoformat(),
                           "path": path, "content": content})
        with self._lock:
            self.posts.parent.mkdir(parents=True, exist_ok=True)
            with self.posts.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class FakeServices:
    """The fake server run on a background thread, for tests and the sandbox.

        with FakeServices(recordings, posts_file) as fakes:
            env["HOUSE_CLERK_BASE_URL"] = fakes.clerk_base_url
    """

    def __init__(self, recordings: Path = DEFAULT_RECORDINGS, posts: Path | None = None,
                 *, port: int = 0, rate_limit_first: int = 0, verbose: bool = False):
        if posts is None:
            raise ValueError("posts: a file to record accepted webhook posts in")
        self.httpd = FakeServicesHTTPServer((HOST, port), recordings, posts,
                                            rate_limit_first, verbose)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    @property
    def clerk_base_url(self) -> str:
        """HOUSE_CLERK_BASE_URL for congress-trades."""
        return f"{self.base_url}/clerk"

    @property
    def sources_base_url(self) -> str:
        """What replaces `{base}` in recordings/sources/sources.json."""
        return f"{self.base_url}/sources"

    @property
    def webhook_url(self) -> str:
        """The `webhook_url` for a MONITOR_CONFIG pointing at this server."""
        return f"{self.base_url}{FAKE_WEBHOOK_PATH}"

    def posts(self) -> list[dict]:
        return read_posts(self.httpd.posts)

    def start(self) -> "FakeServices":
        # A short poll interval: stop() waits for the loop to notice, once per test.
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join()
            self._thread = None
        self.httpd.server_close()

    def __enter__(self) -> "FakeServices":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def read_posts(path: Path) -> list[dict]:
    """Every message the fake webhook accepted, oldest first."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS)
    parser.add_argument("--posts", type=Path, required=True,
                        help="JSONL file each accepted webhook post is appended to")
    parser.add_argument("--rate-limit-first", type=int, default=0, metavar="N",
                        help="Answer the first N webhook posts with 429")
    parser.add_argument("--verbose", action="store_true", help="Log every request")
    args = parser.parse_args()

    fakes = FakeServices(args.recordings, args.posts, port=args.port,
                         rate_limit_first=args.rate_limit_first, verbose=args.verbose)
    # The port first, alone on its line, so a script can read it; then the wiring.
    print(fakes.port, flush=True)
    print(f"Fake services on {fakes.base_url}\n"
          f"  HOUSE_CLERK_BASE_URL={fakes.clerk_base_url}\n"
          f"  sources base ({{base}} in sources.json): {fakes.sources_base_url}\n"
          f"  webhook_url: {fakes.webhook_url}\n"
          f"  posts -> {args.posts}", file=sys.stderr, flush=True)
    try:
        fakes.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        fakes.httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
