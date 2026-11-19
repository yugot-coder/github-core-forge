"""HTTP plumbing: one threading server routing REST, GraphQL, MCP and git
smart-HTTP to their surfaces, with the suite's bearer-token auth and the
headers its protocol checks require.
"""

from __future__ import annotations

import email.utils
import gc
import itertools
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
import json
import os
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import gitproto, graphql, mcp, rest
from .store import ForgeError, Store

SCOPES = "repo, read:org"


class _Headers:
    """Minimal case-insensitive header map for the fast parse path.

    The stock parser routes headers through the email package, whose
    Message set/get are linear scans with per-call case folding — measurably
    the priciest part of a small request. Everything in this codebase (and
    in BaseHTTPRequestHandler's own helpers) reads headers through
    ``.get(name[, default])`` only, so a lower-cased dict is a drop-in:
    duplicate names keep the last value, exactly as Message.__setitem__
    replaces an existing header.
    """

    __slots__ = ("_values",)

    def __init__(self):
        self._values: dict[str, str] = {}

    def __setitem__(self, name: str, value: str) -> None:
        self._values[name.lower()] = value

    def get(self, name: str, default=None):
        return self._values.get(name.lower(), default)

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._values


_STATUS_LINES: dict[int, str] = {}


def _status_line(status: int) -> str:
    line = _STATUS_LINES.get(status)
    if line is None:
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            reason = "Unknown"
        line = f"HTTP/1.1 {status} {reason}\r\n"
        _STATUS_LINES[status] = line
    return line


class App:
    def __init__(self, store: Store):
        self.store = store
        self.token = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
        self._request_counter = itertools.count(1)
        self._counter_lock = threading.Lock()
        # GET response cache: (host, path, query) -> (epoch, status, body,
        # content_type, headers). An entry is served only while the epoch it
        # was stored under is current; every mutating request bumps the
        # epoch, so a cached body can never outlive a state change. The
        # cached bytes are exactly what the uncached path would send.
        self._epoch = 0
        self._get_cache: dict = {}

    def next_request_id(self) -> str:
        with self._counter_lock:
            value = next(self._request_counter)
        return f"{value:08x}-forge"

    def bump_epoch(self) -> None:
        with self._counter_lock:
            self._epoch += 1
        if len(self._get_cache) > 8192:
            self._get_cache.clear()

    def cache_get(self, key):
        entry = self._get_cache.get(key)
        if entry is not None and entry[0] == self._epoch:
            return entry[1]
        return None

    def cache_put(self, key, epoch: int, value, skeleton=None) -> None:
        # value is (status, body, content_type, extra_headers); skeleton,
        # when given, is the response head in three pieces around the two
        # per-request fields (Date value, request id) so a cache hit can be
        # served with a single join + write instead of rebuilding the head.
        self._get_cache[key] = (epoch, value + (skeleton,))

    def authorized(self, headers) -> bool:
        header = headers.get("Authorization")
        if not header:
            return False
        if self.token:
            return header == f"Bearer {self.token}"
        return header.startswith("Bearer ")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "makefun-forge/1.0"

        def setup(self):
            super().setup()
            try:
                # Request/response ping-pong on keep-alive connections stalls
                # for tens of ms per exchange with Nagle + delayed ACK on.
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

        def log_message(self, fmt, *args):  # keep stdout clean
            pass

        # The stock parse_request routes headers through the email package's
        # feedparser, which costs more than the rest of a small request
        # combined. This fast path mirrors CPython 3.12's parse_request for
        # the well-formed case (three-word request line, HTTP/1.0 or 1.1,
        # plain "Name: value" headers) and defers to the stock implementation
        # — before consuming anything — whenever the request line is unusual.
        def parse_request(self):
            requestline = str(self.raw_requestline, "iso-8859-1").rstrip("\r\n")
            words = requestline.split()
            if len(words) != 3 or words[2] not in ("HTTP/1.0", "HTTP/1.1"):
                return super().parse_request()
            self.command = None
            self.request_version = words[2]
            self.close_connection = words[2] == "HTTP/1.0"
            self.requestline = requestline
            self.command, self.path = words[0], words[1]
            if self.path.startswith("//"):
                self.path = "/" + self.path.lstrip("/")
            headers = _Headers()
            count = 0
            while True:
                line = self.rfile.readline(65537)
                if line in (b"\r\n", b"\n", b""):
                    break
                if len(line) > 65536:
                    self.send_error(431, "Line too long")
                    return False
                count += 1
                if count > 100:
                    self.send_error(431, "Too many headers")
                    return False
                name, sep, value = line.decode("iso-8859-1").partition(":")
                if not sep or not name.strip():
                    self.send_error(400, "Bad request syntax (%r)" % requestline)
                    return False
                headers[name.strip()] = value.strip()
            self.headers = headers
            conntype = headers.get("Connection", "")
            if conntype.lower() == "close":
                self.close_connection = True
            elif conntype.lower() == "keep-alive" and self.protocol_version >= "HTTP/1.1":
                self.close_connection = False
            expect = headers.get("Expect", "")
            if (expect.lower() == "100-continue" and self.protocol_version >= "HTTP/1.1"
                    and self.request_version >= "HTTP/1.1"):
                if not self.handle_expect_100():
                    return False
            return True

        _date_cache = [0, ""]
        _date_bytes_cache = [0, b""]

        def date_time_string(self, timestamp=None):
            if timestamp is not None:
                return email.utils.formatdate(timestamp, usegmt=True)
            now = time.time()
            second = int(now)
            cache = Handler._date_cache
            if cache[0] != second:
                cache[0] = second
                cache[1] = email.utils.formatdate(now, usegmt=True)
            return cache[1]

        def _date_bytes(self) -> bytes:
            now = time.time()
            second = int(now)
            cache = Handler._date_bytes_cache
            if cache[0] != second:
                cache[0] = second
                cache[1] = email.utils.formatdate(now, usegmt=True).encode("iso-8859-1")
            return cache[1]

        # -- plumbing ---------------------------------------------------------
        def _read_body(self) -> bytes:
            transfer = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in transfer:
                chunks = bytearray()
                while True:
                    size_line = self.rfile.readline(65536).strip()
                    try:
                        size = int(size_line.split(b";")[0], 16)
                    except ValueError:
                        break
                    if size == 0:
                        # trailers until blank line
                        while True:
                            line = self.rfile.readline(65536)
                            if line in (b"\r\n", b"\n", b""):
                                break
                        break
                    chunks += self.rfile.read(size)
                    self.rfile.readline(65536)
                return bytes(chunks)
            length = self.headers.get("Content-Length")
            if length is None:
                return b""
            return self.rfile.read(int(length))

        _server_version = None

        def _head_skeleton(self, status: int, body_len: int, content_type: str | None,
                           extra: dict | None):
            """The response head as (pre, mid, post) around the Date value and
            the request id — the only two per-request fields. Everything else
            is fixed for a given (status, length, type, extras) shape."""
            if Handler._server_version is None:
                Handler._server_version = self.version_string()
            pre = (_status_line(status)
                   + f"Server: {Handler._server_version}\r\n"
                   + "Date: ").encode("iso-8859-1")
            mid = "\r\n"
            if content_type is not None:
                mid += f"Content-Type: {content_type}\r\n"
            mid += f"Content-Length: {body_len}\r\n"
            mid += "X-GitHub-Request-Id: "
            post = "\r\n"
            for key, value in (extra or {}).items():
                post += f"{key}: {value}\r\n"
            post += "\r\n"
            return (pre, mid.encode("iso-8859-1"), post.encode("iso-8859-1"))

        def _send(self, status: int, body: bytes, content_type: str | None, extra: dict | None = None):
            # One send for the whole response (status line, headers, body):
            # the stock send_response/send_header/end_headers path costs
            # several calls and two socket writes per reply. The bytes on
            # the wire are identical, in the same header order.
            pre, mid, post = self._head_skeleton(status, len(body), content_type, extra)
            request_id = app.next_request_id().encode("iso-8859-1")
            if body and self.command != "HEAD":
                self.wfile.write(b"".join((pre, self._date_bytes(), mid, request_id, post, body)))
            else:
                self.wfile.write(b"".join((pre, self._date_bytes(), mid, request_id, post)))

        def _send_cached(self, skeleton, body: bytes):
            pre, mid, post = skeleton
            request_id = app.next_request_id().encode("iso-8859-1")
            if body and self.command != "HEAD":
                self.wfile.write(b"".join((pre, self._date_bytes(), mid, request_id, post, body)))
            else:
                self.wfile.write(b"".join((pre, self._date_bytes(), mid, request_id, post)))

        def _send_json(self, status: int, payload, extra: dict | None = None):
            if payload is None:
                self._send(status, b"", None, extra)
                return
            body = json.dumps(payload).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", extra)

        # -- routing ------------------------------------------------------------
        def _dispatch(self, method: str):
            try:
                self._route(method)
            except ForgeError as exc:
                self._send_json(exc.status, {"message": exc.message})
            except BrokenPipeError:
                pass
            except Exception:
                traceback.print_exc()
                try:
                    self._send_json(500, {"message": "Internal Server Error"})
                except Exception:
                    pass

        def _route(self, method: str):
            # Fast path: a cached REST GET needs nothing but the raw target.
            if method in ("GET", "HEAD"):
                target = self.path
                if target == "/api/v3" or target.startswith(("/api/v3/", "/api/v3?")):
                    if not app.authorized(self.headers):
                        self._send_json(401, {"message": "Requires authentication"})
                        return
                    host = self.headers.get("Host", "127.0.0.1")
                    hit = app.cache_get((host, target))
                    if hit is not None:
                        status, resp_body, content_type, headers, skeleton = hit
                        if skeleton is not None:
                            self._send_cached(skeleton, resp_body)
                        else:
                            self._send(status, resp_body, content_type, headers)
                        return
            split = urlsplit(self.path)
            raw_path = split.path
            query = parse_qs(split.query)
            segments = [part if "%" not in part else unquote(part)
                        for part in raw_path.split("/") if part != ""]
            host = self.headers.get("Host", "127.0.0.1")
            base_url = f"http://{host}"
            # Always drain the request body first: an early response (401/404)
            # must never leave body bytes to corrupt the next keep-alive request.
            raw_body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""

            if raw_path == "/healthz" and method == "GET":
                self._send_json(200, {"ok": True})
                return

            if raw_path == "/mcp":
                if not app.authorized(self.headers):
                    self._send_json(401, {"message": "Requires authentication"})
                    return
                app.bump_epoch()  # MCP tools can mutate
                status, payload, content_type = mcp.handle_mcp(app.store, raw_body)
                if payload is None:
                    self._send(status, b"", None)
                else:
                    self._send(status, json.dumps(payload).encode("utf-8"), content_type)
                return

            if raw_path == "/api/graphql":
                if not app.authorized(self.headers):
                    self._send_json(401, {"message": "Requires authentication"})
                    return
                if method != "POST":
                    self._send_json(405, {"message": "Method Not Allowed"})
                    return
                try:
                    payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                except Exception:
                    self._send_json(400, {"message": "Problems parsing JSON"})
                    return
                app.bump_epoch()  # GraphQL mutations can mutate
                status, result = graphql.handle_graphql(app.store, payload)
                self._send_json(status, result)
                return

            if raw_path == "/api/v3" or raw_path.startswith("/api/v3/"):
                if not app.authorized(self.headers):
                    self._send_json(401, {"message": "Requires authentication"})
                    return
                cache_key = None
                if method in ("GET", "HEAD"):
                    cache_key = (host, self.path)
                    hit = app.cache_get(cache_key)
                    if hit is not None:
                        status, resp_body, content_type, headers, skeleton = hit
                        if skeleton is not None:
                            self._send_cached(skeleton, resp_body)
                        else:
                            self._send(status, resp_body, content_type, headers)
                        return
                body = None
                if raw_body:
                    try:
                        body = json.loads(raw_body.decode("utf-8"))
                    except Exception:
                        self._send_json(400, {"message": "Problems parsing JSON"})
                        return
                rest_segments = segments[2:] if len(segments) > 2 else []
                ctx = rest.Ctx(app.store, base_url)
                # The epoch is bumped (for mutations) and read (for cache
                # stores) inside the store lock, together with the state
                # access itself, so a cached body always matches the epoch
                # it is tagged with.
                with app.store.lock:
                    if cache_key is None:
                        app.bump_epoch()
                    epoch = app._epoch
                    status, payload, extra = rest.handle(ctx, method, rest_segments, query, body)
                headers = {"X-OAuth-Scopes": SCOPES}
                headers.update(extra or {})
                if payload is None:
                    resp_body, content_type = b"", None
                else:
                    resp_body = json.dumps(payload).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                if cache_key is not None:
                    skeleton = self._head_skeleton(status, len(resp_body), content_type, headers)
                    app.cache_put(cache_key, epoch, (status, resp_body, content_type, headers), skeleton)
                self._send(status, resp_body, content_type, headers)
                return

            # Git smart HTTP: /{owner}/{repo}.git/...
            if len(segments) >= 3 and segments[1].endswith(".git"):
                if not app.authorized(self.headers):
                    self._send_json(401, {"message": "Requires authentication"})
                    return
                owner = segments[0]
                name = segments[1][:-len(".git")]
                repo = app.store.get_repo(owner, name)
                if repo is None:
                    self._send(404, b"not found\n", "text/plain")
                    return
                tail = "/".join(segments[2:])
                if tail == "info/refs" and method == "GET":
                    service = query.get("service", [""])[0]
                    if service not in ("git-upload-pack", "git-receive-pack"):
                        self._send(400, b"bad service\n", "text/plain")
                        return
                    git_protocol = self.headers.get("Git-Protocol", "")
                    if service == "git-upload-pack" and "version=2" in git_protocol:
                        body_out = gitproto.v2_advertisement()
                    else:
                        body_out = gitproto.v0_advertisement(repo, service)
                    self._send(200, body_out, f"application/x-{service}-advertisement")
                    return
                if tail in ("git-upload-pack", "git-receive-pack") and method == "POST":
                    with app.store.lock:
                        if tail == "git-upload-pack":
                            status, content_type, body_out = gitproto.upload_pack(repo, self.headers, raw_body)
                        else:
                            app.bump_epoch()  # pushes mutate objects and refs
                            status, content_type, body_out = gitproto.receive_pack(repo, raw_body)
                    self._send(status, body_out, content_type)
                    return
                self._send(404, b"not found\n", "text/plain")
                return

            self._send_json(404, {"message": "Not Found"})

        do_GET = lambda self: self._dispatch("GET")  # noqa: E731
        do_POST = lambda self: self._dispatch("POST")  # noqa: E731
        do_PUT = lambda self: self._dispatch("PUT")  # noqa: E731
        do_PATCH = lambda self: self._dispatch("PATCH")  # noqa: E731
        do_DELETE = lambda self: self._dispatch("DELETE")  # noqa: E731
        do_HEAD = lambda self: self._dispatch("GET")  # noqa: E731

    return Handler


class ForgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256
    # ThreadingHTTPServer spawns a fresh thread per connection. The clone
    # benchmark alone opens ~150 connections per round, and a thread spawn
    # (a clone syscall, dearer still under a sandboxed runtime) costs more
    # than the small responses it serves. A pre-spawned pool of workers
    # removes the spawn from the request path entirely; the wire behaviour
    # of every connection is unchanged.
    POOL_WORKERS = 128

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pool: ThreadPoolExecutor | None = None

    def start_pool(self) -> None:
        if self._pool is not None:
            return
        pool = ThreadPoolExecutor(max_workers=self.POOL_WORKERS,
                                  thread_name_prefix="forge-http")
        # Pre-spawn every worker: submit tasks that park on an event until
        # all workers exist, then release them.
        gate = threading.Event()
        started = threading.Semaphore(0)

        def _park():
            started.release()
            gate.wait(30)

        futures = [pool.submit(_park) for _ in range(self.POOL_WORKERS)]
        for _ in range(self.POOL_WORKERS):
            started.acquire()
        gate.set()
        for future in futures:
            future.result()
        self._pool = pool

    def process_request(self, request, client_address):
        if self._pool is None:
            super().process_request(request, client_address)
            return
        self._pool.submit(self._pool_task, request, client_address)

    def _pool_task(self, request, client_address):
        # Mirrors ThreadingMixIn.process_request_thread.
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self):
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        super().server_close()


def serve(store: Store, host: str = "", port: int = 0) -> ThreadingHTTPServer:
    # The forge is allocation-heavy and long-lived; the default gen0
    # threshold makes the cyclic GC scan constantly for almost no benefit
    # (the object graph is overwhelmingly acyclic). Raising the thresholds
    # is invisible to every response — it only removes pauses.
    gc.set_threshold(100_000, 100, 100)
    app = App(store)
    server = ForgeHTTPServer((host, port), make_handler(app))
    server.start_pool()
    return server
