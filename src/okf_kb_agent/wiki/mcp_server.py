"""Minimal MCP server for the OKF tool surface.

The default transport speaks newline-delimited JSON-RPC 2.0 over stdio.  An
optional stdlib HTTP transport is provided by
:mod:`okf_kb_agent.wiki.mcp_http`.  It has no third-party dependencies.

Run with::

    uv run okf-mcp --root .
    uv run okf-mcp --root . --transport http --port 8765

The server exposes the contracts in :mod:`okf_kb_agent.wiki.tooling`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from .. import __version__
from .tooling import TOOL_SPECS, ToolError, dispatch_tool

__all__ = ["PROTOCOL_VERSION", "handle_message", "main", "serve"]

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "okf-mcp"
SERVER_VERSION = __version__


def handle_message(message: Any, *, root: str | Path) -> dict[str, Any] | None:
    """Handle one JSON-RPC message and return a response or ``None``."""
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request")
    request_id = message.get("id")
    is_notification = "id" not in message
    if message.get("jsonrpc") != "2.0":
        return _error(request_id, -32600, "jsonrpc must be '2.0'")
    method = message.get("method")
    if not isinstance(method, str):
        return _error(request_id, -32600, "method must be a string")

    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        return None

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Call okf_context first for bounded, Gotchas-filtered context. "
                "Use okf_search/okf_traverse for targeted discovery and "
                "okf_validate after mutations. Markdown remains the source of "
                "truth; relative Markdown links only."
            ),
        }
        return _reply(request_id, result, is_notification=is_notification)

    if method == "ping":
        return _reply(request_id, {}, is_notification=is_notification)

    if method == "tools/list":
        return _reply(request_id, {"tools": TOOL_SPECS},
                      is_notification=is_notification)

    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict):
            return _fail(request_id, -32602, "params must be an object",
                         is_notification=is_notification)
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _fail(request_id, -32602, "tool name is required",
                         is_notification=is_notification)
        if not isinstance(arguments, dict):
            return _fail(request_id, -32602, "arguments must be an object",
                         is_notification=is_notification)
        try:
            value = dispatch_tool(name, arguments, root=root)
            content = _tool_content(value, is_error=False)
        except ToolError as exc:
            content = _tool_content(str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            content = _tool_content(
                f"{type(exc).__name__}: {exc}", is_error=True)
        return _reply(request_id, content, is_notification=is_notification)

    if is_notification:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def serve(root: str | Path, instream: TextIO | None = None,
          outstream: TextIO | None = None) -> None:
    """Serve newline-delimited JSON-RPC until EOF."""
    source = instream or sys.stdin
    sink = outstream or sys.stdout
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error(None, -32700, f"parse error: {exc.msg}")
        else:
            try:
                response = handle_message(message, root=root)
            except Exception as exc:  # noqa: BLE001 - keep server alive
                response = _error(
                    message.get("id") if isinstance(message, dict) else None,
                    -32603, f"internal error: {type(exc).__name__}: {exc}")
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False,
                              separators=(",", ":")) + "\n")
        sink.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="okf-mcp",
        description="MCP server for the OKF knowledge base")
    parser.add_argument("--root", default=".",
                        help="allowed wiki root (default: current directory)")
    parser.add_argument("--transport", choices=("stdio", "http"),
                        default="stdio", help="stdio (default) or HTTP")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind host (default: loopback)")
    parser.add_argument("--port", type=int, default=8765,
                        help="HTTP port (default: 8765; 0 picks a free port)")
    parser.add_argument("--token", default=os.environ.get("OKF_MCP_TOKEN"),
                        help="bearer token for HTTP (default: OKF_MCP_TOKEN)")
    parser.add_argument("--version", action="version", version=SERVER_VERSION)
    args = parser.parse_args(argv)
    try:
        if args.transport == "http":
            from .mcp_http import serve_http
            serve_http(Path(args.root), host=args.host, port=args.port,
                       token=args.token)
        else:
            serve(Path(args.root))
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    except (OSError, ValueError) as exc:
        print(f"okf-mcp: {exc}", file=sys.stderr)
        return 2
    return 0


def _reply(request_id: Any, result: dict[str, Any], *,
           is_notification: bool) -> dict[str, Any] | None:
    if is_notification:
        return None
    return _result(request_id, result)


def _fail(request_id: Any, code: int, message: str, *,
          is_notification: bool) -> dict[str, Any] | None:
    if is_notification:
        return None
    return _error(request_id, code, message)


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_content(value: Any, *, is_error: bool) -> dict[str, Any]:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
