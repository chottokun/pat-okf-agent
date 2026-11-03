"""Tests for the stdio MCP server and its JSON-RPC boundary."""
from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from pat_okf_agent.wiki.mcp_server import (
    PROTOCOL_VERSION,
    handle_message,
    serve,
)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def node(meta: str, body: str = "# Note\n") -> str:
    return f"---\n{meta}---\n{body}"


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wiki = self.root / "wiki"
        write(self.wiki, "index.md", "# Index\n")
        write(self.wiki, "a.md", node(
            "type: Note\ntitle: Amorphous alloy\n",
            "# Amorphous alloy\nA metallic glass.\n"))

    def request(self, method: str, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        return handle_message(message, root=self.root)

    def test_initialize(self):
        response = self.request("initialize")
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        result = response["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(result["serverInfo"]["name"], "okf-mcp")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("okf_context", result["instructions"])

    def test_tools_list_and_call(self):
        listed = self.request("tools/list")
        tools = listed["result"]["tools"]
        self.assertEqual(len(tools), 7)
        self.assertEqual({tool["name"] for tool in tools}, {
            "okf_search", "okf_context", "okf_traverse", "okf_validate",
            "okf_stats", "okf_index", "okf_ingest_epo",
        })
        called = self.request("tools/call", {
            "name": "okf_search",
            "arguments": {"wiki": "wiki", "query": "amorphous"},
        })
        self.assertFalse(called["result"]["isError"])
        hits = json.loads(called["result"]["content"][0]["text"])
        self.assertEqual(hits[0]["node"], "a.md")

    def test_tool_error_is_returned_as_content(self):
        called = self.request("tools/call", {
            "name": "okf_traverse",
            "arguments": {"wiki": "wiki", "seed": "missing"},
        })
        self.assertTrue(called["result"]["isError"])
        self.assertIn("seed not found",
                      called["result"]["content"][0]["text"])
        escaped = self.request("tools/call", {
            "name": "okf_stats",
            "arguments": {"wiki": "../outside"},
        })
        self.assertTrue(escaped["result"]["isError"])
        self.assertIn("escapes", escaped["result"]["content"][0]["text"])

        db_escaped = self.request("tools/call", {
            "name": "okf_search",
            "arguments": {"wiki": "wiki", "query": "amorphous",
                          "db_path": "../escape.db"},
        })
        self.assertTrue(db_escaped["result"]["isError"])
        self.assertIn("db_path escapes the allowed root",
                      db_escaped["result"]["content"][0]["text"])

    def test_protocol_errors_and_notifications(self):
        self.assertIsNone(handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            root=self.root))
        self.assertIsNone(handle_message(
            {"jsonrpc": "2.0", "method": "initialize"}, root=self.root))
        bad_version = handle_message(
            {"jsonrpc": "1.0", "id": 9, "method": "ping"}, root=self.root)
        self.assertEqual(bad_version["error"]["code"], -32600)
        unknown = self.request("unknown/method")
        self.assertEqual(unknown["error"]["code"], -32601)
        bad_call = self.request("tools/call", {"arguments": {}})
        self.assertEqual(bad_call["error"]["code"], -32602)
        bad_params = self.request("tools/call", [])
        self.assertEqual(bad_params["error"]["code"], -32602)
        invalid = handle_message("not an object", root=self.root)
        self.assertEqual(invalid["error"]["code"], -32600)

    def test_ping(self):
        self.assertEqual(self.request("ping")["result"], {})

    def test_serve_handles_lines_and_parse_errors(self):
        source = StringIO(
            '{"jsonrpc":"2.0","id":"a","method":"ping"}\n'
            '\n'
            'not json\n'
            '{"jsonrpc":"2.0","id":"b","method":"tools/list"}\n')
        sink = StringIO()
        serve(self.root, instream=source, outstream=sink)
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(responses[0]["id"], "a")
        self.assertEqual(responses[0]["result"], {})
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertEqual(len(responses[2]["result"]["tools"]), 7)


if __name__ == "__main__":
    unittest.main()
