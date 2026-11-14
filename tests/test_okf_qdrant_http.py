"""End-to-end HTTP tests for the Pattern C adapters and CLI.

The unit tests inject fake transports.  These tests exercise the real stdlib
``urllib`` transport against small in-process HTTP servers, so request shapes,
URL construction, auth headers, JSON parsing and CLI wiring are covered without
requiring external Qdrant or embedding infrastructure.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path

from okf_kb_agent.wiki.cli import main
from okf_kb_agent.wiki.embeddings import GemmaEmbedder, HttpEmbedder
from okf_kb_agent.wiki.mcp_http import make_server
from okf_kb_agent.wiki.qdrant_backend import QdrantBackend


def write(root: Path, rel: str, meta: str, body: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{meta}---\n{body}", encoding="utf-8")
    return path


def vector_for(text: str) -> list[float]:
    folded = text.casefold()
    return [
        float("metallic" in folded or "glass" in folded
              or "amorphous" in folded),
        float("zeolite" in folded),
        0.1,
    ]


class Server:
    """Run a handler class on a loopback ephemeral port in a daemon thread."""

    def __init__(self, handler, state):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.httpd.state = state
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"


class EmbeddingState:
    def __init__(self):
        self.requests: list[dict] = []


class EmbeddingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.state.requests.append({
            "path": self.path,
            "body": body,
            "authorization": self.headers.get("Authorization"),
        })
        if "embedContent" in self.path:
            text = body["content"]["parts"][0]["text"]
            payload = {"embedding": {"values": vector_for(text)}}
        else:
            text = body.get("input", "")
            payload = {"data": [{"embedding": vector_for(text)}]}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class QdrantState:
    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.requests: list[tuple[str, str, str | None]] = []


class QdrantHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):  # noqa: N802 - stdlib handler API
        self._dispatch("GET")

    def do_PUT(self):  # noqa: N802 - stdlib handler API
        self._dispatch("PUT")

    def do_POST(self):  # noqa: N802 - stdlib handler API
        self._dispatch("POST")

    def do_DELETE(self):  # noqa: N802 - stdlib handler API
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        length = int(self.headers.get("Content-Length", "0"))
        body = (json.loads(self.rfile.read(length).decode("utf-8"))
                if length else None)
        state: QdrantState = self.server.state
        state.requests.append((method, parsed.path,
                               self.headers.get("api-key")))
        if len(parts) < 2 or parts[0] != "collections":
            self._send(404, {"status": {"error": "not found"}})
            return
        name = urllib.parse.unquote(parts[1])
        collection = state.collections.get(name)
        if len(parts) == 2:
            if method == "GET":
                if collection is None:
                    self._send(404, {"status": {"error": "not found"}})
                    return
                self._send(200, {"result": {"config": {"params": {
                    "vectors": {"size": collection["size"]}}}}})
                return
            if method == "PUT":
                size = body["vectors"]["size"]
                state.collections[name] = {"size": size, "points": {}}
                self._send(200, {"result": True})
                return
            if method == "DELETE":
                existed = state.collections.pop(name, None) is not None
                self._send(200 if existed else 404, {"result": existed})
                return
        if collection is None:
            self._send(404, {"status": {"error": "not found"}})
            return
        if len(parts) == 3 and parts[2] == "points":
            if method != "PUT":
                self._send(405, {"status": {"error": "method not allowed"}})
                return
            for point in body["points"]:
                collection["points"][point["id"]] = point
            self._send(200, {"result": {"status": "completed"}})
            return
        if len(parts) == 4 and parts[2] == "points":
            if parts[3] == "scroll" and method == "POST":
                self._send(200, {"result": {
                    "points": list(collection["points"].values()),
                    "next_page_offset": None,
                }})
                return
            if parts[3] == "delete" and method == "POST":
                for point_id in body["points"]:
                    collection["points"].pop(point_id, None)
                self._send(200, {"result": {"status": "completed"}})
                return
            if parts[3] == "search" and method == "POST":
                query = body["vector"]
                qnorm = math.sqrt(sum(value * value for value in query))
                hits = []
                for point in collection["points"].values():
                    vector = point["vector"]
                    dot = sum(a * b for a, b in zip(query, vector))
                    vnorm = math.sqrt(sum(value * value for value in vector))
                    score = dot / (qnorm * vnorm) if qnorm and vnorm else 0.0
                    hits.append({
                        "id": point["id"], "score": score,
                        "payload": point["payload"],
                    })
                hits.sort(key=lambda item: -item["score"])
                self._send(200, {"result": hits[:body.get("limit", 10)]})
                return
        self._send(405, {"status": {"error": "method not allowed"}})

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class HttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        old_no_proxy = os.environ.get("NO_PROXY")
        old_no_proxy_l = os.environ.get("no_proxy")
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"

        def restore_proxy():
            if old_no_proxy is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = old_no_proxy
            if old_no_proxy_l is None:
                os.environ.pop("no_proxy", None)
            else:
                os.environ["no_proxy"] = old_no_proxy_l

        self.addCleanup(restore_proxy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "a.md", "type: Note\ntitle: Amorphous alloy\n",
              "# Amorphous alloy\nMetallic glass cools rapidly.\n")
        write(self.root, "b.md", "type: Note\ntitle: Zeolite\n",
              "# Zeolite\nPorous material.\n")

    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(arg) for arg in args])
        return code, out.getvalue(), err.getvalue()

    def test_http_embedders_use_real_urllib_transport(self):
        with Server(EmbeddingHandler, EmbeddingState()) as server:
            openai = HttpEmbedder(
                server.base_url + "/v1/embeddings",
                model="gemma-embedding-2", api_key="secret")
            self.assertEqual(openai("metallic glass"), [1.0, 0.0, 0.1])
            gemma = GemmaEmbedder(
                server.base_url + "/v1beta/models/{model}:embedContent",
                api_key="google-key")
            self.assertEqual(gemma("zeolite"), [0.0, 1.0, 0.1])
            requests = server.httpd.state.requests
        self.assertEqual(requests[0]["path"], "/v1/embeddings")
        self.assertEqual(requests[0]["authorization"], "Bearer secret")
        self.assertEqual(requests[0]["body"]["model"], "gemma-embedding-2")
        self.assertIn("gemma-embedding-2", requests[1]["path"])
        self.assertIn("key=google-key", requests[1]["path"])
        self.assertNotIn("model", requests[1]["body"])

    def test_qdrant_adapter_uses_real_urllib_transport(self):
        with Server(QdrantHandler, QdrantState()) as server:
            backend = QdrantBackend(
                self.root, embedder=vector_for, url=server.base_url,
                api_key="qdrant-secret", auto_sync=False)
            summary = backend.sync()
            self.assertEqual(summary["indexed"], 2)
            self.assertEqual(summary["dimensions"], 3)
            self.assertTrue(any(
                key == "qdrant-secret"
                for _, _, key in server.httpd.state.requests))
            self.assertEqual(backend.search("metallic glass")[0].node, "a.md")
            write(self.root, "b.md", "type: Note\ntitle: Zeolite\n",
                  "# Zeolite\nZeolite and amorphous composite.\n")
            summary = backend.sync()
            self.assertEqual(summary["indexed"], 1)
            self.assertEqual(summary["unchanged"], 1)
            (self.root / "a.md").unlink()
            summary = backend.sync()
            self.assertEqual(summary["removed"], 1)
            self.assertEqual(backend.stats()["points"], 1)
            (self.root / "b.md").unlink()
            self.assertEqual(backend.sync()["points"], 0)
            self.assertNotIn(backend.collection, server.httpd.state.collections)

    def test_cli_qdrant_and_search_with_real_http(self):
        with Server(EmbeddingHandler, EmbeddingState()) as embeddings, \
                Server(QdrantHandler, QdrantState()) as qdrant:
            common = [
                "--embedding-url", embeddings.base_url + "/v1/embeddings",
                "--qdrant-url", qdrant.base_url,
            ]
            code, out, err = self.run_cli(
                "qdrant", self.root, *common, "--force", "--json")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["points"], 2)
            # One probe + one remaining concept; the CLI must not auto-sync
            # before the explicit sync.
            self.assertEqual(
                len(embeddings.httpd.state.requests), 2)
            code, out, err = self.run_cli(
                "search", self.root, "metallic glass",
                "--backend", "qdrant", *common, "--json")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)[0]["node"], "a.md")

    def test_mcp_http_tool_uses_qdrant_backend(self):
        with Server(EmbeddingHandler, EmbeddingState()) as embeddings, \
                Server(QdrantHandler, QdrantState()) as qdrant:
            mcp = make_server(self.root, port=0)
            thread = threading.Thread(target=mcp.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = mcp.server_address[:2]
                payload = {
                    "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": "okf_search", "arguments": {
                        "wiki": ".",
                        "query": "metallic glass",
                        "backend": "qdrant",
                        "embedding_url": embeddings.base_url + "/v1/embeddings",
                        "qdrant_url": qdrant.base_url,
                    }},
                }
                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request("POST", "/mcp", body=json.dumps(payload), headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                })
                response = conn.getresponse()
                data = json.loads(response.read().decode("utf-8"))
                conn.close()
            finally:
                mcp.shutdown()
                mcp.server_close()
                thread.join(timeout=2)
        self.assertEqual(response.status, 200)
        result = data["result"]
        self.assertFalse(result["isError"])
        hits = json.loads(result["content"][0]["text"])
        self.assertEqual(hits[0]["node"], "a.md")


if __name__ == "__main__":
    unittest.main()
