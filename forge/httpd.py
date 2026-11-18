"""HTTP plumbing: one threading server routing REST, GraphQL, MCP and git
smart-HTTP to their surfaces, with the suite's bearer-token auth and the
headers its protocol checks require.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import gitproto, graphql, mcp, rest
from .store import ForgeError, Store

SCOPES = "repo, read:org"


class App:
    def __init__(self, store: Store):
        self.store = store
        self.token = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
        self._request_counter = itertools.count(1)
        self._counter_lock = threading.Lock()

    def next_request_id(self) -> str:
        with self._counter_lock:
            value = next(self._request_counter)
        return f"{value:08x}-forge"

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

        def _send(self, status: int, body: bytes, content_type: str | None, extra: dict | None = None):
            self.send_response(status)
            if content_type is not None:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-GitHub-Request-Id", app.next_request_id())
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

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
            split = urlsplit(self.path)
            raw_path = split.path
            query = parse_qs(split.query)
            segments = [unquote(part) for part in raw_path.split("/") if part != ""]
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
                status, result = graphql.handle_graphql(app.store, payload)
                self._send_json(status, result)
                return

            if raw_path == "/api/v3" or raw_path.startswith("/api/v3/"):
                if not app.authorized(self.headers):
                    self._send_json(401, {"message": "Requires authentication"})
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
                with app.store.lock:
                    status, payload, extra = rest.handle(ctx, method, rest_segments, query, body)
                headers = {"X-OAuth-Scopes": SCOPES}
                headers.update(extra or {})
                if payload is None:
                    self._send(status, b"", None, headers)
                else:
                    self._send_json(status, payload, headers)
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


def serve(store: Store, host: str = "", port: int = 0) -> ThreadingHTTPServer:
    app = App(store)
    server = ForgeHTTPServer((host, port), make_handler(app))
    return server
