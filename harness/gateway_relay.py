"""Model access for a containerised agent without giving it any credentials.

The container's omp is configured with `transport: pi-native`, so every model request
is a POST to `<baseUrl>/v1/pi/stream`. This relay answers that one route by forwarding
the body to `omp auth-gateway stdio` -- the gateway on this host's own credentials --
and returns the gateway's answer.

Why a relay rather than mounting omp's config into the container: ~/.omp/agent holds
the session transcripts and history next to the credentials, and those transcripts
describe the hidden scenarios. Nothing from that directory crosses into the container;
only model responses do.

Bound to the Docker bridge address with a per-run bearer token, so only the containers
this harness starts -- not the LAN, not other local processes without the token -- can
spend the quota.
"""

import itertools
import json
import secrets
import subprocess
import threading
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STREAM_PATH = "/v1/pi/stream"
REQUEST_TIMEOUT = 600


def gateway_body(body: dict) -> dict:
    """The pi-native client names the model `modelId`; the stdio gateway resolves only
    `model` (it answers 404 Unknown model for `modelId`), so the field is renamed."""
    body = dict(body)
    if "modelId" in body and "model" not in body:
        body["model"] = body.pop("modelId")
    return body


class StdioGateway:
    """One `omp auth-gateway stdio` process, multiplexed by request id."""

    def __init__(self, command=("omp", "auth-gateway", "stdio")):
        self.proc = subprocess.Popen(list(command), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, bufsize=1)
        self.pending: dict[int, Future] = {}
        self.ids = itertools.count(1)
        self.write_lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            future = self.pending.pop(reply.get("id"), None)
            if future:
                future.set_result(reply)
        for future in self.pending.values():
            future.set_exception(RuntimeError("gateway exited"))

    def request(self, path: str, body: dict) -> dict:
        request_id = next(self.ids)
        future: Future = Future()
        self.pending[request_id] = future
        with self.write_lock:
            self.proc.stdin.write(json.dumps({"id": request_id, "path": path,
                                              "body": body}) + "\n")
            self.proc.stdin.flush()
        return future.result(timeout=REQUEST_TIMEOUT)

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def make_handler(gateway: StdioGateway, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, message: str) -> None:
            self._reply(status, json.dumps({"error": {"type": "relay_error",
                                                      "message": message}}).encode(),
                        "application/json")

        def do_POST(self):
            if self.headers.get("Authorization") != f"Bearer {token}":
                return self._error(401, "bad or missing relay token")
            if self.path.rstrip("/") != STREAM_PATH:
                return self._error(404, f"only {STREAM_PATH} is relayed")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return self._error(400, "body is not JSON")
            try:
                reply = gateway.request(STREAM_PATH, gateway_body(body))
            except Exception as exc:
                return self._error(502, f"gateway failed: {exc}")
            status, payload = reply.get("status", 502), reply.get("body")
            if isinstance(payload, str):
                # A streamed answer arrives whole, as the SSE text it would have been.
                self._reply(status, payload.encode(), "text/event-stream")
            else:
                self._reply(status, json.dumps(payload).encode(), "application/json")

        def do_GET(self):
            if self.path == "/healthz":
                return self._reply(200, b"ok", "text/plain")
            return self._error(404, "not relayed")

    return Handler


class Relay:
    """`with Relay(host) as relay:` -> relay.base_url, relay.token."""

    def __init__(self, host: str = "172.17.0.1", port: int = 0, gateway_command=None):
        self.token = secrets.token_urlsafe(32)
        self.gateway = StdioGateway(gateway_command or ("omp", "auth-gateway", "stdio"))
        self.server = ThreadingHTTPServer((host, port), make_handler(self.gateway, self.token))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "Relay":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.gateway.close()
