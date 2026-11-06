"""Tests for the shared EPO OPS / Publication Server client (no network)."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from pat_okf_agent.epo import (
    EpoApiError,
    EpsClient,
    OpsClient,
    OpsCredentials,
    RateLimiter,
    normalize_docdb,
    parse_biblio,
    parse_eps_claims,
    parse_family,
    parse_ops_claims,
    parse_search,
)

TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
API_ROOT = "https://ops.epo.org/3.2/rest-services"


class FakeResponse:
    def __init__(self, body: bytes | str, status: int = 200, headers=None):
        self._body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.headers = headers or {}

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTransport:
    """Queue responses by URL fragment; record requests."""

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


def _token_response(token="test-token"):
    return FakeResponse(json.dumps({
        "access_token": token,
        "expires_in": 1199,
        "api_product_list": "[ops-prod]",
    }))


def _search_response():
    return FakeResponse(json.dumps({
        "ops:world-patent-data": {
            "ops:biblio-search": {
                "@total-result-count": "128",
                "ops:search-result": {
                    "ops:publication-reference": [
                        {"@family-id": "1", "document-id": {
                            "@document-id-type": "docdb",
                            "country": {"$": "WO"},
                            "doc-number": {"$": "9999999"},
                            "kind": {"$": "A1"},
                        }},
                        {"@family-id": "2", "document-id": {
                            "@document-id-type": "docdb",
                            "country": {"$": "JP"},
                            "doc-number": {"$": "9999999"},
                            "kind": {"$": "A"},
                        }},
                    ]
                },
            }
        }
    }))


def _biblio_response():
    return FakeResponse(json.dumps({
        "ops:world-patent-data": {
            "exchange-documents": {
                "exchange-document": {
                    "@family-id": "99999999",
                    "@country": "EP",
                    "@doc-number": "9999998",
                    "@kind": "A1",
                    "bibliographic-data": {
                        "publication-reference": {"document-id": {
                            "@document-id-type": "docdb",
                            "country": {"$": "EP"},
                            "doc-number": {"$": "9999998"},
                            "kind": {"$": "A1"},
                            "date": {"$": "20140326"},
                        }},
                        "parties": {
                            "applicants": {"applicant": [
                                {"applicant-name": {"name": {"$": "EXAMPLE CORP"}}},
                                {"applicant-name": {"name": {"$": "EXAMPLE KOGYO KK"}}},
                            ]},
                            "inventors": {"inventor": [
                                {"inventor-name": {"name": {"$": "TARO EXAMPLE"}}},
                            ]},
                        },
                        "invention-title": [
                            {"$": "METALLIC POWDER PRODUCTION METHOD", "@lang": "en"},
                            {"$": "VERFAHREN ZUR HERSTELLUNG", "@lang": "de"},
                        ],
                        "priority-claims": {"priority-claim": {
                            "document-id": {
                                "@document-id-type": "docdb",
                                "country": {"$": "JP"},
                                "doc-number": {"$": "2011110904"},
                                "kind": {"$": "A"},
                                "date": {"$": "20110518"},
                            }
                        }},
                    },
                }
            }
        }
    }))


def _family_response():
    return FakeResponse(json.dumps({
        "ops:world-patent-data": {
            "ops:patent-family": {
                "@total-result-count": "2",
                "ops:family-member": [
                    {"publication-reference": {"document-id": {
                        "@document-id-type": "docdb",
                        "country": {"$": "WO"},
                        "doc-number": {"$": "9999999"},
                        "kind": {"$": "A1"},
                    }}},
                    {"publication-reference": {"document-id": {
                        "@document-id-type": "docdb",
                        "country": {"$": "EP"},
                        "doc-number": {"$": "9999998"},
                        "kind": {"$": "A1"},
                    }}},
                ],
            }
        }
    }))


def _claims_response():
    return FakeResponse(json.dumps({
        "ops:world-patent-data": {
            "ftxt:fulltext-documents": {
                "ftxt:fulltext-document": {
                    "claims": {
                        "@lang": "EN",
                        "claim": {"claim-text": [
                            {"$": "1. A method of making metal powder."},
                            {"$": "wherein the jet is annular."},
                            {"$": "2. The method of claim 1, wherein the jet is hot."},
                        ]},
                    }
                }
            }
        }
    }))


class RateLimiterStateTests(unittest.TestCase):
    """Cross-process spacing via the shared state file (no network)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "rate_state.json"

    def test_state_file_serializes_instances_with_min_interval(self):
        sleeps: list[float] = []
        wall = [1000.0]

        def make_limiter():
            return RateLimiter(
                3.0,
                clock=lambda: wall[0],
                sleeper=sleeps.append,
                state_path=self.state,
                wall=lambda: wall[0],
            )

        limiter_a = make_limiter()
        limiter_a.wait()                      # first request: no sleep, stamps state
        self.assertEqual(sleeps, [])
        self.assertEqual(self.state.read_text().strip(), "1000.000000")

        wall[0] = 1000.5                      # a "second process" 0.5s later
        limiter_b = make_limiter()
        limiter_b.wait()                      # must sleep 3.0 - 0.5 = 2.5
        self.assertEqual(sleeps, [2.5])
        self.assertEqual(self.state.read_text().strip(), "1000.500000")

    def test_stale_state_is_never_a_stall(self):
        sleeps: list[float] = []
        self.state.write_text("900.000000\n")  # long-past timestamp
        wall = [1000.0]
        limiter = RateLimiter(
            3.0,
            clock=lambda: wall[0],
            sleeper=sleeps.append,
            state_path=self.state,
            wall=lambda: wall[0],
        )
        limiter.wait()
        self.assertEqual(sleeps, [])
        self.assertEqual(self.state.read_text().strip(), "1000.000000")

    def test_wait_state_oserror_falls_back_to_in_process_rate_limit(self):
        sleeps: list[float] = []
        clock = [1000.0]
        # Make a file where a directory is expected, causing mkdir to raise OSError
        barrier = Path(self.tmp.name) / "cannot_create_dir"
        barrier.write_text("blocking file")
        unusable_path = barrier / "rate_state.json"

        limiter = RateLimiter(
            3.0,
            clock=lambda: clock[0],
            sleeper=sleeps.append,
            state_path=unusable_path,
            wall=lambda: clock[0],
        )
        limiter.wait()  # first call sets _last to 1000.0
        self.assertEqual(sleeps, [])
        clock[0] = 1001.0
        limiter.wait()  # second call at 1001.0 falls back to in-process wait: sleeps 2.0s
        self.assertEqual(sleeps, [2.0])


class OpsClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "ops"
        self.token_cache = Path(self.tmp.name) / "token.json"
        self.now = 1_000_000.0

    def _client(self, responses):
        self.transport = FakeTransport(responses)
        return OpsClient(
            credentials={
                "consumer_key": "key",
                "consumer_secret": "secret",
                "token_url": TOKEN_URL,
                "api_root": API_ROOT,
            },
            token_cache=self.token_cache,
            cache_dir=self.cache,
            sleep_s=0,
            transports=self.transport,
            limiter=RateLimiter(0),
            clock=lambda: self.now,
            sleeper=lambda _seconds: None,
        )

    def test_search_parses_and_uses_cache(self):
        client = self._client([
            (TOKEN_URL, _token_response()),
            ("/published-data/search", _search_response()),
        ])
        result = client.search('pa="Example Industry"', name="q4")
        self.assertEqual(result.total_count, 128)
        self.assertEqual(result.docdbs, ["WO.9999999.A1", "JP.9999999.A"])
        self.assertEqual(len(self.transport.requests), 2)

        # Second call is served from the raw cache, including the cached token.
        result2 = client.search('pa="Example Industry"', name="q4")
        self.assertEqual(result2.total_count, 128)
        self.assertEqual(len(self.transport.requests), 2)

    def test_token_is_cached_across_client_instances(self):
        client = self._client([(TOKEN_URL, _token_response())])
        self.assertEqual(client.get_token(), "test-token")
        self.assertEqual(client.get_token(), "test-token")
        self.assertEqual(len(self.transport.requests), 1)

        client2 = self._client([(TOKEN_URL, _token_response("should-not-be-used"))])
        self.assertEqual(client2.get_token(), "test-token")
        self.assertEqual(len(self.transport.requests), 0)

    def test_search_rejects_free_plan_excess_window(self):
        client = self._client([])
        with self.assertRaises(ValueError):
            client.search('pa="Example Industry"', end=26)
        with self.assertRaises(ValueError):
            client.register_search('pa="Example Industry"', end=26)

    def test_biblio_and_family(self):
        client = self._client([
            (TOKEN_URL, _token_response()),
            ("/biblio", _biblio_response()),
            ("/family/", _family_response()),
        ])
        rec = client.biblio("EP9999998A1")
        self.assertEqual(rec["title"], "METALLIC POWDER PRODUCTION METHOD")
        self.assertEqual(rec["applicants"], ["EXAMPLE CORP", "EXAMPLE KOGYO KK"])
        self.assertEqual(rec["priority"], "JP2011110904A20110518")
        self.assertEqual(rec["family_id"], "99999999")
        family = client.family("WO9999999A1")
        self.assertEqual([p.docdb for p in family],
                         ["WO.9999999.A1", "EP.9999998.A1"])

    def test_family_uses_root_family_service(self):
        client = self._client([
            (TOKEN_URL, _token_response()),
            ("/family/", _family_response()),
        ])
        client.family("WO9999999A1")
        family_urls = [r.full_url for r in self.transport.requests
                       if "/family/" in r.full_url]
        self.assertEqual(len(family_urls), 1)
        self.assertTrue(family_urls[0].startswith(
            API_ROOT + "/family/publication/docdb/"), family_urls[0])
        self.assertNotIn("/published-data/family", family_urls[0])

    def test_claims_split_numbered_fragments(self):
        client = self._client([
            (TOKEN_URL, _token_response()),
            ("/claims", _claims_response()),
        ])
        claims = client.claims("WO9999999A1")
        self.assertEqual(len(claims), 2)
        self.assertEqual(
            claims[0],
            "1. A method of making metal powder. wherein the jet is annular.")
        self.assertTrue(claims[1].startswith("2."))

    def test_register_uses_epodoc_without_kind(self):
        client = self._client([
            (TOKEN_URL, _token_response()),
            ("/register/patent/EP9999998/biblio", FakeResponse("{}")),
        ])
        client.register("EP9999998A1")
        urls = [r.full_url for r in self.transport.requests]
        self.assertTrue(any("register/patent/EP9999998/biblio" in u for u in urls))

    def test_http_error_is_raised_and_not_cached_as_json(self):
        client = self._client([
            (TOKEN_URL, _token_response()),
            ("/biblio", lambda: (_ for _ in ()).throw(
                urllib.error.HTTPError(
                    "https://example.test", 404, "Not Found", {},
                    io.BytesIO(b"<fault>missing</fault>")))),
        ])
        with self.assertRaises(EpoApiError) as ctx:
            client.biblio("EP9999999A1")
        self.assertEqual(ctx.exception.status, 404)
        self.assertFalse((self.cache / "biblio_EP_9999999_A1.json").exists())
        self.assertTrue((self.cache / "biblio_EP_9999999_A1.error.txt").exists())

    def test_dynamic_cache_staleness_warning_over_24h(self):
        import os
        import sys
        import time

        client = self._client([
            (TOKEN_URL, _token_response()),
            ("/published-data/search", _search_response),
            ("/published-data/publication/docdb/EP.9999999.A1/biblio", _biblio_response),
        ])
        client.search("ta=metal")
        search_files = list(self.cache.glob("search_*.json"))
        self.assertTrue(search_files)
        search_file = search_files[0]

        # Artificially age the cache file to 25 hours ago
        stale_time = time.time() - (25 * 3600)
        os.utime(search_file, (stale_time, stale_time))

        stderr_buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_buf
        try:
            client.search("ta=metal")
        finally:
            sys.stderr = old_stderr

        warning = stderr_buf.getvalue()
        self.assertIn("older than 24 hours", warning)
        self.assertIn("--refresh", warning)

        # Fresh cache (< 24h) emits no warning
        os.utime(search_file, (time.time(), time.time()))
        stderr_buf = io.StringIO()
        sys.stderr = stderr_buf
        try:
            client.search("ta=metal")
        finally:
            sys.stderr = old_stderr
        self.assertEqual(stderr_buf.getvalue(), "")

        # Static endpoint (biblio) emits no warning even when > 24h
        client.biblio("EP9999999A1")
        biblio_files = list(self.cache.glob("biblio_*.json"))
        self.assertTrue(biblio_files)
        os.utime(biblio_files[0], (stale_time, stale_time))
        stderr_buf = io.StringIO()
        sys.stderr = stderr_buf
        try:
            client.biblio("EP9999999A1")
        finally:
            sys.stderr = old_stderr
        self.assertEqual(stderr_buf.getvalue(), "")


class ParsingTests(unittest.TestCase):
    def test_normalize_docdb(self):
        cases = {
            "EP9999999B1": "EP.9999999.B1",
            "EP.9999999.B1": "EP.9999999.B1",
            "EP 9999999 B1": "EP.9999999.B1",
            "WO9999999A1": "WO.9999999.A1",
            "JP9999999A": "JP.9999999.A",
            "TW I760101 B": "TW.I760101.B",
            "EP9999999": "EP.9999999",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_docdb(raw), expected)

    def test_parse_search_deduplicates(self):
        data = json.loads(_search_response().read())
        result = parse_search(data, "q")
        self.assertEqual(result.total_count, 128)
        self.assertEqual(len(result.publications), 2)

    def test_parse_biblio_accepts_top_level_title(self):
        data = {"ops:world-patent-data": {"exchange-documents": {"exchange-document": {
            "@country": "TW", "@doc-number": "I760101", "@kind": "B",
            "invention-title": {"$": "Top-level title", "@lang": "en"},
            "bibliographic-data": {},
        }}}}
        self.assertEqual(parse_biblio(data)["title"], "Top-level title")

    def test_parse_family(self):
        refs = parse_family(json.loads(_family_response().read()))
        self.assertEqual([p.docdb for p in refs],
                         ["WO.9999999.A1", "EP.9999998.A1"])

    def test_parse_ops_claims(self):
        claims = parse_ops_claims(json.loads(_claims_response().read()))
        self.assertEqual(len(claims), 2)


class EpsClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "eps"

    def test_eps_document_and_claims(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ep-patent-document xmlns="http://www.epo.org/exchange" '
            'country="EP" doc-number="9999999" kind="B1" date-publ="20190102">'
            "<claims><claim><claim-text>1. A powder.</claim-text></claim>"
            "<claim><claim-text>2. The powder of claim 1.</claim-text></claim></claims>"
            "</ep-patent-document>"
        )
        transport = FakeTransport([
            ("/patents/EP9999999NWB1/document.xml", FakeResponse(xml)),
        ])
        eps = EpsClient(cache_dir=self.cache, sleep_s=0, transports=transport,
                        sleeper=lambda _s: None)
        info = eps.info("EP9999999B1")
        self.assertEqual(info["publication_number"], "EP9999999B1")
        self.assertEqual(info["date_publ"], "20190102")
        self.assertEqual(info["claims"], ["1. A powder.", "2. The powder of claim 1."])
        self.assertEqual(eps.claims("EP9999999B1"), info["claims"])
        self.assertEqual(len(transport.requests), 1)  # second call used cache

    def test_eps_url_requires_kind(self):
        self.assertEqual(
            EpsClient.document_url("EP9999999", kind="B1"),
            "https://data.epo.org/publication-server/rest/v1.2/patents/EP9999999NWB1/document.xml",
        )
        with self.assertRaises(ValueError):
            EpsClient.document_url("EP9999999")

    def test_eps_invalid_xml(self):
        with self.assertRaises(EpoApiError):
            parse_eps_claims("<not-xml")

    def test_eps_exists_404_and_500(self):
        transport_404 = FakeTransport([
            ("/patents/EP9999999NWB1/document.xml", lambda: (_ for _ in ()).throw(
                urllib.error.HTTPError(
                    "https://data.epo.org", 404, "Not Found", {},
                    io.BytesIO(b"<fault>missing</fault>")))),
        ])
        eps_404 = EpsClient(cache_dir=self.cache, sleep_s=0, transports=transport_404,
                            sleeper=lambda _s: None)
        self.assertFalse(eps_404.exists("EP9999999", kind="B1"))

        transport_500 = FakeTransport([
            ("/patents/EP9999999NWB1/document.xml", lambda: (_ for _ in ()).throw(
                urllib.error.HTTPError(
                    "https://data.epo.org", 500, "Internal Server Error", {},
                    io.BytesIO(b"<fault>server error</fault>")))),
        ])
        eps_500 = EpsClient(cache_dir=self.cache, sleep_s=0, transports=transport_500,
                            sleeper=lambda _s: None)
        with self.assertRaises(EpoApiError) as ctx:
            eps_500.exists("EP9999999", kind="B1")
        self.assertEqual(ctx.exception.status, 500)


class CredentialTests(unittest.TestCase):
    def test_env_credentials_without_file(self):
        creds = OpsCredentials.load(
            path="/does/not/exist.json",
            environ={"EPO_OPS_CONSUMER_KEY": "k", "EPO_OPS_CONSUMER_SECRET": "s",
                     "EPO_OPS_API_ROOT": API_ROOT},
        )
        self.assertEqual(creds.consumer_key, "k")
        self.assertEqual(creds.api_root, API_ROOT)
        self.assertEqual(creds.source, "environment")

    def test_file_credentials(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "ops_credentials.json"
        path.write_text(json.dumps({"consumer_key": "k", "consumer_secret": "s"}))
        creds = OpsCredentials.load(path=path, environ={})
        self.assertEqual(creds.token_urls[0], "https://ops.epo.org/3.2/auth/accesstoken")
        self.assertEqual(creds.source, str(path))


class CliAndRootTests(unittest.TestCase):
    def test_root_is_cwd_not_repo_parent(self):
        from pat_okf_agent import epo
        self.assertEqual(epo.ROOT, Path.cwd())

    def test_cli_error_outputs_to_stderr(self):
        import io
        import sys
        from pat_okf_agent.epo import main

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        old_stderr = sys.stderr
        old_stdout = sys.stdout
        sys.stderr = stderr_buf
        sys.stdout = stdout_buf
        try:
            code = main(["--credentials", "/nonexistent/credentials.json", "search", "ta=metal"])
        finally:
            sys.stderr = old_stderr
            sys.stdout = old_stdout
        self.assertEqual(code, 2)
        self.assertIn("error:", stderr_buf.getvalue())
        self.assertEqual(stdout_buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
