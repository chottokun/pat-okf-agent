"""Stdlib HTTP transport for the OKF MCP server.

This is a deliberately small Streamable-HTTP-compatible endpoint:

- ``POST /mcp`` accepts one JSON-RPC message and returns JSON (or a single
  ``text/event-stream`` event when requested)
- ``GET /health`` is an unauthenticated liveness probe
- notifications return ``202 Accepted`` with no body
- an optional bearer token protects every ``/mcp`` request

It is not a multi-session SSE server.  For local agent use this keeps the
security surface small; a reverse proxy can add TLS and network policy.
"""
from __future__ import annotations

import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .mcp_server import SERVER_VERSION, handle_message

__all__ = ["MAX_BODY", "make_server", "serve_http"]

MAX_BODY = 1_048_576
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def make_server(root: str | Path, *, host: str = "127.0.0.1",
                port: int = 8765, token: str | None = None,
                max_body: int = MAX_BODY) -> ThreadingHTTPServer:
    """Create a threaded HTTP server; call ``serve_forever`` to run it."""
    if host not in _LOOPBACK and not token:
        raise ValueError("refusing non-loopback HTTP without a bearer token")
    if max_body < 1:
        raise ValueError("max_body must be >= 1")

    class Handler(BaseHTTPRequestHandler):
        server_version = f"okf-mcp/{SERVER_VERSION}"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path.split("?", 1)[0] == "/health":
                self._json(200, {"ok": True, "version": SERVER_VERSION})
                return
            if self.path.split("?", 1)[0] in ("/mcp", "/"):
                self._method_not_allowed("POST")
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path.split("?", 1)[0] not in ("/mcp", "/"):
                self._json(404, {"error": "not found"})
                return
            if not self._authorized():
                self._json(401, {"error": "unauthorized"},
                           headers={"WWW-Authenticate": "Bearer"})
                return
            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header or "0")
            except ValueError:
                self._json(400, {"error": "invalid Content-Length"})
                return
            if length <= 0 or length > max_body:
                self._json(413 if length > max_body else 400,
                           {"error": "request body missing or too large"})
                return
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                self._json(415, {"error": "Content-Type must be application/json"})
                return
            raw = self.rfile.read(length)
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json(200, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700,
                              "message": f"parse error: {exc}"},
                })
                return
            try:
                response = handle_message(message, root=root_path)
            except Exception as exc:  # noqa: BLE001 - keep endpoint alive
                response = {
                    "jsonrpc": "2.0",
                    "id": message.get("id") if isinstance(message, dict) else None,
                    "error": {"code": -32603,
                              "message": f"internal error: "
                                         f"{type(exc).__name__}: {exc}"},
                }
            if response is None:
                self._empty(202)
                return
            accept = self.headers.get("Accept", "")
            if "text/event-stream" in accept and "application/json" not in accept:
                self._sse(response)
            else:
                self._json(200, response)

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path.split("?", 1)[0] not in ("/mcp", "/"):
                self._json(404, {"error": "not found"})
                return
            if not self._authorized():
                self._json(401, {"error": "unauthorized"},
                           headers={"WWW-Authenticate": "Bearer"})
                return
            self._empty(204)

        def _authorized(self) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                return False
            return hmac.compare_digest(header[len(prefix):], token)

        def _json(self, status: int, payload: dict[str, Any],
                  headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status >= 400:
                self.send_header("Connection", "close")
                self.close_connection = True
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, payload: dict[str, Any]) -> None:
            body = ("event: message\ndata: " + json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"))
                + "\n\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _method_not_allowed(self, allow: str) -> None:
            self._json(405, {"error": "method not allowed"},
                       headers={"Allow": allow})

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write("okf-mcp: " + (format % args) + "\n")

    root_path = Path(root).resolve()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.root = root_path  # type: ignore[attr-defined]
    server.token = token  # type: ignore[attr-defined]
    return server


def serve_http(root: str | Path, *, host: str = "127.0.0.1",
               port: int = 8765, token: str | None = None) -> None:
    """Run the HTTP transport until interrupted."""
    server = make_server(root, host=host, port=port, token=token)
    hostname, actual_port = server.server_address[:2]
    print(f"okf-mcp listening on http://{hostname}:{actual_port}/mcp",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
