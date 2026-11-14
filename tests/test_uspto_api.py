"""Offline tests for the USPTO Open Data Portal client (no network).

TDD: these tests define the contract of ``okf_kb_agent.uspto``:
credential resolution, URL/parameter construction, cache-first behaviour,
401 guidance, 429 retry, and the conservative result window.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from okf_kb_agent.uspto import (
    API_ROOT,
    UsptoApiError,
    UsptoClient,
    UsptoCredentials,
    UsptoCredentialsError,
    parse_data_bag,
)

TOKEN_KEY = "test-api-key"


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.headers = headers or {}

    def read(self, _n=-1):
        return self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        for fragment, response in self.responses:
            if fragment in request.full_url:
                if callable(response):
                    return response()
                return response
        raise AssertionError(f"unexpected URL: {request.full_url}")


def _search_payload():
    return {
        "count": 1,
        "patentFileWrapperDataBag": [{
            "applicationNumberText": "18999999",
            "applicationMetaData": {
                "inventionTitle": "TEST COMPOSITION",
                "filingDate": "2022-10-10",
                "firstInventorName": "JANE EXAMPLE",
            },
        }],
    }


class CredentialTests(unittest.TestCase):
    def test_env_key_is_used(self):
        creds = UsptoCredentials.load(env={"USPTO_API_KEY": "env-key"})
        self.assertEqual(creds.api_key, "env-key")

    def test_missing_key_raises(self):
        with self.assertRaises(UsptoCredentialsError):
            UsptoCredentials.load(env={})

    def test_credentials_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            path.write_text(json.dumps({"api_key": "file-key"}))
            creds = UsptoCredentials.load(path, env={})
            self.assertEqual(creds.api_key, "file-key")


class UsptoClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "uspto"
        self.state = Path(self.tmp.name) / "rate.json"

    def _client(self, responses, key=TOKEN_KEY):
        self.transport = FakeTransport(responses)
        return UsptoClient(
            api_key=key,
            cache_dir=self.cache,
            sleep_s=0,
            transports=self.transport,
            limiter=None,  # created from sleep_s=0 + state below
            state_path=self.state,
        )

    def test_search_builds_query_and_caches(self):
        client = self._client([("/applications/search", FakeResponse(json.dumps(_search_payload())))])
        result = client.search("pa=18999999", limit=10)
        self.assertEqual(result["count"], 1)
        url = self.transport.requests[0].full_url
        self.assertIn("/api/v1/patent/applications/search", url)
        self.assertIn("q=pa%3D18999999", url)
        self.assertIn("limit=10", url)
        self.assertIn("start=0", url)

        client.search("pa=18999999", limit=10)
        self.assertEqual(len(self.transport.requests), 1)  # cache hit

    def test_api_key_header_is_sent(self):
        client = self._client([("/applications/search", FakeResponse(json.dumps(_search_payload())))])
        client.search("q=test")
        request = self.transport.requests[0]
        self.assertEqual(request.get_header("X-api-key"), TOKEN_KEY)

    def test_401_explains_missing_or_invalid_key(self):
        def unauthorized():
            raise urllib.error.HTTPError(
                "https://api.uspto.gov/api/v1/patent/applications/search",
                401, "Unauthorized", {}, FakeResponse('{"message":"Unauthorized"}'))
        client = self._client([("/applications/search", unauthorized)])
        with self.assertRaises(UsptoApiError) as ctx:
            client.search("q=test")
        self.assertIn("401", str(ctx.exception))
        self.assertIn("API key", str(ctx.exception))

    def test_403_also_explains_key_problem(self):
        def forbidden():
            raise urllib.error.HTTPError(
                "https://api.uspto.gov/api/v1/patent/applications/search",
                403, "Forbidden", {}, FakeResponse('{"message":"Forbidden"}'))
        client = self._client([("/applications/search", forbidden)])
        with self.assertRaises(UsptoApiError) as ctx:
            client.search("q=test")
        self.assertIn("403", str(ctx.exception))
        self.assertIn("API key", str(ctx.exception))

    def test_429_is_retried_with_retry_after(self):
        calls = {"n": 0}

        def throttled_once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "https://api.uspto.gov/api/v1/patent/applications/search",
                    429, "Too Many Requests", {"Retry-After": "0"},
                    FakeResponse("too many"))
            return FakeResponse(json.dumps(_search_payload()))

        sleeps = []
        client = self._client([("/applications/search", throttled_once)])
        client._sleep = sleeps.append
        result = client.search("q=test")
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(self.transport.requests), 2)
        self.assertTrue(sleeps, "retry should wait before retrying")

    def test_limit_window_is_conservative(self):
        client = self._client([])
        with self.assertRaises(ValueError):
            client.search("q=test", limit=26)

    def test_application_and_subresources_paths(self):
        body = json.dumps({"count": 1, "patentFileWrapperDataBag": [{"ok": True}]})
        client = self._client([
            ("/applications/18999999/continuity", FakeResponse(body)),
            ("/applications/18999999", FakeResponse(body)),
        ])
        client.continuity("18999999")
        client.application("18/999,999")
        urls = [r.full_url for r in self.transport.requests]
        self.assertTrue(urls[0].endswith("/api/v1/patent/applications/18999999/continuity"))
        self.assertTrue(urls[1].endswith("/api/v1/patent/applications/18999999"))

    def test_parse_data_bag(self):
        self.assertEqual(parse_data_bag(_search_payload())[0]["applicationNumberText"], "18999999")
        self.assertEqual(parse_data_bag({}), [])

    def test_offline_without_cache_raises(self):
        from okf_kb_agent.uspto import UsptoOfflineError
        client = UsptoClient(api_key=TOKEN_KEY, cache_dir=self.cache,
                             sleep_s=0, offline=True)
        with self.assertRaises(UsptoOfflineError):
            client.search("q=test")


if __name__ == "__main__":
    unittest.main()
