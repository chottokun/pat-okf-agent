"""Tests for the optional HTTP embedding adapters."""
from __future__ import annotations

import json
import unittest
import urllib.request

from okf_kb_agent.wiki.embeddings import GemmaEmbedder, HttpEmbedder
from okf_kb_agent.wiki.search import BackendUnavailable


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class RecordingTransport:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


class EmbeddingTests(unittest.TestCase):
    def test_openai_compatible_request_and_response(self):
        transport = RecordingTransport(
            FakeResponse({"data": [{"embedding": [1, 2, 3]}]}))
        embedder = HttpEmbedder(
            "http://embed/v1/embeddings", model="gemma-embedding-2",
            api_key="secret", transport=transport)
        self.assertEqual(embedder("hello"), [1.0, 2.0, 3.0])
        request, timeout = transport.requests[0]
        self.assertEqual(request.full_url, "http://embed/v1/embeddings")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body, {"input": "hello", "model": "gemma-embedding-2"})
        self.assertEqual(timeout, 60.0)

    def test_gemini_style_model_url_and_api_key(self):
        transport = RecordingTransport(
            FakeResponse({"embedding": {"values": [4, 5]}}))
        embedder = GemmaEmbedder(
            "https://example/v1beta/models/{model}:embedContent",
            api_key="key", transport=transport)
        self.assertEqual(embedder("特許"), [4.0, 5.0])
        request, _ = transport.requests[0]
        self.assertIn("gemma-embedding-2", request.full_url)
        self.assertIn("key=key", request.full_url)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["content"]["parts"][0]["text"], "特許")
        self.assertNotIn("model", body)

    def test_http_error_and_bad_payload_fail_closed(self):
        transport = RecordingTransport(FakeResponse({"error": "no"}, status=500))
        embedder = HttpEmbedder("http://embed", transport=transport)
        with self.assertRaises(BackendUnavailable):
            embedder("x")
        empty = RecordingTransport(FakeResponse({"data": []}))
        with self.assertRaises(BackendUnavailable):
            HttpEmbedder("http://embed", transport=empty)("x")

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):
            HttpEmbedder("ftp://embed")
        with self.assertRaises(ValueError):
            HttpEmbedder("http://embed", style="bogus")


if __name__ == "__main__":
    unittest.main()
