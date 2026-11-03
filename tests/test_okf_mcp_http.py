"""Tests for the stdlib HTTP/SSE MCP transport."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from pat_okf_agent.wiki.mcp_http import make_server
from pat_okf_agent.wiki.mcp_server import main


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class McpHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "wiki/index.md", "# Index\n")
        write(self.root, "wiki/a.md",
              "---\ntype: Note\ntitle: Amorphous\n---\n# Amorphous\nGlass.\n")
        self.server = make_server(self.root, port=0, token="secret")
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, payload=None, *,
                token: str | None = "secret", accept: str = "application/json",
                content_type: str = "application/json"):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Accept": accept, "Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        body = None if payload is None else json.dumps(payload)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        conn.close()
        return response.status, dict(response.getheaders()), data

    def test_health_is_open(self):
        status, _, body = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_initialize_requires_bearer_token(self):
        status, headers, _ = self.request(
            "POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            token=None)
        self.assertEqual(status, 401)
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")
        status, _, body = self.request(
            "POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["serverInfo"]["name"],
                         "okf-mcp")

    def test_tools_call_and_notification(self):
        status, _, body = self.request(
            "POST", "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "okf_search",
                        "arguments": {"wiki": "wiki", "query": "amorphous"}}})
        self.assertEqual(status, 200)
        result = json.loads(body)["result"]
        self.assertFalse(result["isError"])
        hits = json.loads(result["content"][0]["text"])
        self.assertEqual(hits[0]["node"], "a.md")
        status, _, body = self.request(
            "POST", "/mcp",
            {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertEqual(body, "")

    def test_sse_and_parse_errors(self):
        status, headers, body = self.request(
            "POST", "/mcp",
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            accept="text/event-stream")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn("event: message\ndata: ", body)
        status, _, body = self.request("POST", "/mcp", None)
        self.assertEqual(status, 400)
        status, _, body = self.request(
            "POST", "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "ping"},
            content_type="text/plain")
        self.assertEqual(status, 415)

    def test_delete_requires_token(self):
        status, _, _ = self.request("DELETE", "/mcp", token=None)
        self.assertEqual(status, 401)
        status, _, _ = self.request("DELETE", "/mcp")
        self.assertEqual(status, 204)

    def test_non_loopback_requires_token(self):
        with self.assertRaises(ValueError):
            make_server(self.root, host="0.0.0.0", port=0, token=None)
        self.assertEqual(main([
            "--root", str(self.root), "--transport", "http",
            "--host", "0.0.0.0", "--port", "0",
        ]), 2)


if __name__ == "__main__":
    unittest.main()
