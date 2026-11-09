"""Tests for OKF source verification (okf verify-source)."""

from __future__ import annotations

import unittest

from pat_okf_agent.wiki.model import parse_source_entry
from pat_okf_agent.wiki.verify import (
    ClassifiedTarget,
    EvidenceRecord,
    SourceApiError,
    SourceMetadata,
    SourceOfflineError,
    SourceRateLimitError,
    SourceVerifier,
    SourceVerifyError,
    VerificationChecks,
    VerificationResult,
    classify_target,
)


class TestClassifyTarget(unittest.TestCase):
    def test_classify_bare_doi(self) -> None:
        c = classify_target("10.1016/S1002-0071(12)60029-X")
        self.assertEqual(c.identifier_type, "doi")
        self.assertEqual(c.identifier, "10.1016/s1002-0071(12)60029-x")
        self.assertEqual(c.kind, "paper")

    def test_classify_doi_urls_and_prefixes(self) -> None:
        for prefix in ("https://doi.org/", "http://dx.doi.org/", "doi:"):
            c = classify_target(f"{prefix}10.1111/jace.14748.")
            self.assertEqual(c.identifier_type, "doi")
            self.assertEqual(c.identifier, "10.1111/jace.14748")
            self.assertEqual(c.kind, "paper")

    def test_classify_doi_with_explicit_kind_prefix(self) -> None:
        c = classify_target("paper:10.1143/jjap.48.05eb03")
        self.assertEqual(c.identifier_type, "doi")
        self.assertEqual(c.identifier, "10.1143/jjap.48.05eb03")
        self.assertEqual(c.kind, "paper")

    def test_classify_arxiv_modern_and_old(self) -> None:
        c1 = classify_target("2401.12345")
        self.assertEqual(c1.identifier_type, "arxiv")
        self.assertEqual(c1.identifier, "arXiv:2401.12345")
        self.assertEqual(c1.kind, "preprint")

        c2 = classify_target("arXiv:2401.12345v2")
        self.assertEqual(c2.identifier_type, "arxiv")
        self.assertEqual(c2.identifier, "arXiv:2401.12345v2")

        c3 = classify_target("cond-mat/0501234")
        self.assertEqual(c3.identifier_type, "arxiv")
        self.assertEqual(c3.identifier, "arXiv:cond-mat/0501234")

    def test_classify_arxiv_urls(self) -> None:
        c1 = classify_target("https://arxiv.org/abs/2401.12345")
        self.assertEqual(c1.identifier_type, "arxiv")
        self.assertEqual(c1.identifier, "arXiv:2401.12345")

        c2 = classify_target("https://arxiv.org/pdf/2401.12345.pdf")
        self.assertEqual(c2.identifier_type, "arxiv")
        self.assertEqual(c2.identifier, "arXiv:2401.12345")

    def test_classify_web_url_and_kind(self) -> None:
        c1 = classify_target("https://example.com/aln-datasheet.html#sec1")
        self.assertEqual(c1.identifier_type, "url")
        self.assertEqual(c1.identifier, "https://example.com/aln-datasheet.html")
        self.assertEqual(c1.kind, "web")

        c2 = classify_target("datasheet:https://example.com/aln-datasheet.pdf")
        self.assertEqual(c2.identifier_type, "url")
        self.assertEqual(c2.identifier, "https://example.com/aln-datasheet.pdf")
        self.assertEqual(c2.kind, "datasheet")

        c3 = classify_target("https://example.com/report.pdf", kind="standard")
        self.assertEqual(c3.kind, "standard")

    def test_classify_invalid(self) -> None:
        for invalid in ("", "   ", "ftp://example.com", "not-an-id", "just_a_word"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    classify_target(invalid)


class TestVerificationResultModel(unittest.TestCase):
    def test_format_okf_source_success(self) -> None:
        meta = SourceMetadata(
            title="AlN Ceramics",
            authors=["Taro Example"],
            publisher="Wiley",
            year=2017,
        )
        res = VerificationResult(
            ok=True,
            status="verified",
            input="paper:10.1111/jace.14748",
            identifier="10.1111/jace.14748",
            identifier_type="doi",
            kind="paper",
            registration_agency="crossref",
            via="crossref",
            retrieved_at="2026-11-08",
            issued="2017-01-12",
            metadata=meta,
            checks=VerificationChecks(title_match=True),
            evidence=EvidenceRecord(cached=False, sha256="sha256:abc"),
            okf_source=None,  # will be computed or tested
        )
        okf_src = res.format_okf_source()
        self.assertEqual(
            okf_src,
            "paper:10.1111/jace.14748 issued=2017-01-12 retrieved=2026-11-08 via=crossref",
        )
        # Check that it passes model.parse_source_entry() without error
        entry = parse_source_entry(okf_src)
        self.assertEqual(entry.kind, "paper")
        self.assertEqual(entry.id, "10.1111/jace.14748")
        self.assertEqual(entry.attrs.get("issued"), "2017-01-12")
        self.assertEqual(entry.attrs.get("retrieved"), "2026-11-08")
        self.assertEqual(entry.attrs.get("via"), "crossref")

    def test_format_okf_source_without_issued(self) -> None:
        meta = SourceMetadata(title="AlN Paper", year=2017)
        res = VerificationResult(
            ok=True,
            status="verified",
            input="10.1111/jace.14748",
            identifier="10.1111/jace.14748",
            identifier_type="doi",
            kind="paper",
            registration_agency="crossref",
            via="crossref",
            retrieved_at="2026-11-08",
            issued=None,  # Year only, no full date
            metadata=meta,
            checks=VerificationChecks(),
            evidence=EvidenceRecord(),
            okf_source=None,
        )
        okf_src = res.format_okf_source()
        self.assertEqual(
            okf_src,
            "paper:10.1111/jace.14748 retrieved=2026-11-08 via=crossref",
        )
        entry = parse_source_entry(okf_src)
        self.assertNotIn("issued", entry.attrs)

    def test_format_okf_source_not_verified(self) -> None:
        res = VerificationResult(
            ok=False,
            status="not_found",
            input="10.1111/nonexistent",
            identifier="10.1111/nonexistent",
            identifier_type="doi",
            kind="paper",
            registration_agency=None,
            via="crossref",
            retrieved_at="2026-11-08",
            issued=None,
            metadata=SourceMetadata(),
            checks=VerificationChecks(),
            evidence=EvidenceRecord(),
            okf_source=None,
        )
        self.assertIsNone(res.format_okf_source())

    def test_as_dict_contract(self) -> None:
        meta = SourceMetadata(publisher="Wiley", year=2020)
        res = VerificationResult(
            ok=True,
            status="verified",
            input="10.1111/jace.14748",
            identifier="10.1111/jace.14748",
            identifier_type="doi",
            kind="paper",
            registration_agency="crossref",
            via="crossref",
            retrieved_at="2026-11-08",
            issued=None,
            metadata=meta,
            checks=VerificationChecks(),
            evidence=EvidenceRecord(sha256="sha256:123"),
            okf_source="paper:10.1111/jace.14748 retrieved=2026-11-08 via=crossref",
            warnings=["test warning"],
        )
        d = res.as_dict()
        self.assertTrue(d["ok"])
        self.assertEqual(d["status"], "verified")
        self.assertEqual(d["metadata"]["publisher"], "Wiley")
        self.assertEqual(d["evidence"]["sha256"], "sha256:123")
        self.assertEqual(d["warnings"], ["test warning"])
        self.assertNotIn("trust_score", d)
        self.assertNotIn("confidence", d)


class TestExceptions(unittest.TestCase):
    def test_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(SourceOfflineError, SourceVerifyError))
        self.assertTrue(issubclass(SourceRateLimitError, SourceVerifyError))
        self.assertTrue(issubclass(SourceApiError, SourceVerifyError))

        err = SourceApiError(status=404, url="https://example.com", body="Not Found")
        self.assertEqual(err.status, 404)
        self.assertEqual(err.url, "https://example.com")
        self.assertEqual(err.body, "Not Found")
        self.assertIn("404", str(err))


class FakeResponse:
    def __init__(
        self,
        body: bytes | str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ):
        self._body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.headers = headers or {}
        self.url = url

    def geturl(self) -> str | None:
        return self.url

    def read(self, n: int = -1) -> bytes:
        if n == -1 or n >= len(self._body):
            return self._body
        return self._body[:n]

    def getheader(self, name: str, default: str | None = None) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class FakeTransport:
    def __init__(self, responses: list[tuple[str, Any]]):
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        self.requests.append(request)
        for fragment, resp in self.responses:
            if fragment in request.full_url:
                if callable(resp):
                    return resp()
                return resp
        raise AssertionError(f"unexpected URL: {request.full_url}")


import hashlib
import json
import tempfile
from pathlib import Path


class TestDoiVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_verifier(self, transport: Any, **kwargs: Any) -> SourceVerifier:
        params: dict[str, Any] = {
            "cache_dir": self.cache_dir,
            "transports": transport,
            "sleeper": lambda s: None,
            "sleep": 0.0,
            "no_rate_state": True,
        }
        params.update(kwargs)
        return SourceVerifier(**params)

    def test_crossref_success_full_date_and_cache(self) -> None:
        cr_payload = {
            "status": "ok",
            "message": {
                "title": ["High Thermal Conductivity of Aluminum Nitride Ceramics"],
                "author": [
                    {"given": "Taro", "family": "Example"},
                    {"name": "Hanako Sample"},
                ],
                "publisher": "Wiley",
                "container-title": ["Journal of the American Ceramic Society"],
                "type": "journal-article",
                "published-print": {"date-parts": [[2017, 1, 12]]},
                "abstract": "<jats:p>AlN sintered with Y2O3 reached 180 W/m·K.</jats:p>",
                "URL": "https://doi.org/10.1111/jace.14748",
            },
        }
        transport = FakeTransport([
            ("api.crossref.org/works/", FakeResponse(json.dumps(cr_payload))),
        ])
        verifier = self._make_verifier(transport)

        # 1st call
        res = verifier.verify("10.1111/jace.14748")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "verified")
        self.assertEqual(res.registration_agency, "crossref")
        self.assertEqual(res.via, "crossref")
        self.assertEqual(res.issued, "2017-01-12")
        self.assertEqual(res.metadata.year, 2017)
        self.assertEqual(res.metadata.publisher, "Wiley")
        self.assertEqual(res.metadata.authors, ["Taro Example", "Hanako Sample"])
        self.assertEqual(
            res.metadata.abstract, "AlN sintered with Y2O3 reached 180 W/m·K."
        )
        self.assertFalse(res.evidence.cached)
        self.assertEqual(len(transport.requests), 1)

        # 2nd call: cache hit
        res2 = verifier.verify("10.1111/jace.14748")
        self.assertTrue(res2.ok)
        self.assertTrue(res2.evidence.cached)
        self.assertEqual(len(transport.requests), 1)  # No new network request

    def test_crossref_year_only(self) -> None:
        cr_payload = {
            "status": "ok",
            "message": {
                "title": ["Some Paper"],
                "published-print": {"date-parts": [[2020]]},
                "publisher": "Elsevier",
            },
        }
        transport = FakeTransport([
            ("api.crossref.org/works/", FakeResponse(json.dumps(cr_payload))),
        ])
        verifier = self._make_verifier(transport)
        res = verifier.verify("10.1016/j.ceramint.2020.01")
        self.assertTrue(res.ok)
        self.assertIsNone(res.issued)
        self.assertEqual(res.metadata.year, 2020)
        self.assertEqual(res.metadata.publisher, "Elsevier")
        self.assertNotIn("issued=", res.okf_source or "")

    def test_crossref_retracted(self) -> None:
        cr_payload = {
            "status": "ok",
            "message": {
                "title": ["Fabricated Results in Ceramic Sintering"],
                "update-to": [{"type": "retraction", "updated": {"timestamp": 123}}],
            },
        }
        transport = FakeTransport([
            ("api.crossref.org/works/", FakeResponse(json.dumps(cr_payload))),
        ])
        verifier = self._make_verifier(transport)
        res = verifier.verify("10.1111/jace.bad")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "mismatch")
        self.assertTrue(res.metadata.is_retracted)

    def test_crossref_404_fallback_to_jalc(self) -> None:
        import urllib.error

        jalc_payload = {
            "data": {
                "title_list": [
                    {"title": "窒化アルミニウム基板の熱伝導率評価", "lang": "ja"},
                    {"title": "Evaluation of AlN Substrates", "lang": "en"},
                ],
                "creator_list": [{"creator": "山田 太郎"}],
                "publisher_list": [{"publisher": "日本セラミックス協会"}],
                "publication_date": {"year": "2019", "month": "04", "day": "01"},
                "url": "https://www.jstage.jst.go.jp/article/...",
            }
        }

        def raise_404():
            raise urllib.error.HTTPError(
                "https://api.crossref.org/works/10.2109/test", 404, "Not Found", {}, None
            )

        transport = FakeTransport([
            ("api.crossref.org/works/", raise_404),
            ("doi.org/ra/", FakeResponse(json.dumps([{"DOI": "10.2109/test", "RA": "JaLC"}]))),
            ("api.japanlinkcenter.org/dois/", FakeResponse(json.dumps(jalc_payload))),
        ])
        verifier = self._make_verifier(transport)
        res = verifier.verify("10.2109/test")
        self.assertTrue(res.ok)
        self.assertEqual(res.registration_agency, "jalc")
        self.assertEqual(res.via, "jalc")
        self.assertEqual(res.metadata.publisher, "日本セラミックス協会")
        self.assertIn("Evaluation of AlN Substrates", res.metadata.alternate_titles)
        self.assertEqual(res.issued, "2019-04-01")

    def test_crossref_404_fallback_to_datacite_csl(self) -> None:
        import urllib.error

        csl_payload = {
            "title": "Dataset of AlN Thermal Properties",
            "author": [{"family": "Smith", "given": "John"}],
            "publisher": "Dryad",
            "issued": {"date-parts": [[2021, 6, 15]]},
        }

        def raise_404():
            raise urllib.error.HTTPError(
                "https://api.crossref.org/works/10.5061/dryad.123", 404, "Not Found", {}, None
            )

        transport = FakeTransport([
            ("api.crossref.org/works/", raise_404),
            ("doi.org/ra/", FakeResponse(json.dumps([{"DOI": "10.5061/dryad.123", "RA": "DataCite"}]))),
            ("doi.org/10.5061/dryad.123", FakeResponse(json.dumps(csl_payload))),
        ])
        verifier = self._make_verifier(transport)
        res = verifier.verify("10.5061/dryad.123")
        self.assertTrue(res.ok)
        self.assertEqual(res.registration_agency, "datacite")
        self.assertEqual(res.metadata.publisher, "Dryad")
        self.assertEqual(res.issued, "2021-06-15")

    def test_all_404_not_found(self) -> None:
        import urllib.error

        def raise_404():
            raise urllib.error.HTTPError(
                "https://api.crossref.org/works/10.9999/none", 404, "Not Found", {}, None
            )

        transport = FakeTransport([
            ("api.crossref.org/works/", raise_404),
            ("doi.org/ra/", FakeResponse(json.dumps([{"DOI": "10.9999/none", "status": "DOI does not exist"}]))),
        ])
        verifier = self._make_verifier(transport)
        res = verifier.verify("10.9999/none")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "not_found")
        # Ensure 404 response is NOT cached
        cache_files = list(self.cache_dir.glob("**/*.json"))
        self.assertEqual(len(cache_files), 0)

    def test_mailto_in_request_and_ua(self) -> None:
        cr_payload = {
            "status": "ok",
            "message": {"title": ["Test"]},
        }
        transport = FakeTransport([
            ("api.crossref.org/works/", FakeResponse(json.dumps(cr_payload))),
        ])
        verifier = self._make_verifier(
            transport,
            mailto="researcher@example.com",
        )
        verifier.verify("10.1111/jace.14748")
        req = transport.requests[0]
        self.assertIn("mailto=researcher%40example.com", req.full_url)
        ua = req.headers.get("User-agent", "")
        self.assertIn("mailto:researcher@example.com", ua)

    def test_rate_limit_retry_and_exhaustion(self) -> None:
        import urllib.error

        calls = [0]

        def raise_429():
            calls[0] += 1
            headers = {"Retry-After": "1"}
            raise urllib.error.HTTPError(
                "https://api.crossref.org/works/10.1111/rl",
                429,
                "Too Many Requests",
                headers,  # type: ignore
                None,
            )

        cr_payload = {"status": "ok", "message": {"title": ["Success After Retry"]}}

        def succeed_on_retry():
            calls[0] += 1
            if calls[0] == 1:
                headers = {"Retry-After": "0"}
                raise urllib.error.HTTPError(
                    "https://api.crossref.org/works/10.1111/rl",
                    429,
                    "Too Many Requests",
                    headers,  # type: ignore
                    None,
                )
            return FakeResponse(json.dumps(cr_payload))

        # First case: retry once and succeed
        transport1 = FakeTransport([("api.crossref.org/works/", succeed_on_retry)])
        verifier1 = self._make_verifier(transport1)
        res = verifier1.verify("10.1111/rl")
        self.assertTrue(res.ok)
        self.assertEqual(calls[0], 2)

        # Second case: exhausted retries -> SourceRateLimitError
        transport2 = FakeTransport([("api.crossref.org/works/", raise_429)])
        verifier2 = self._make_verifier(transport2, retries=1)
        with self.assertRaises(SourceRateLimitError):
            verifier2.verify("10.1111/rl2")

    def test_offline_mode(self) -> None:
        transport = FakeTransport([])
        verifier = self._make_verifier(transport, offline=True)
        with self.assertRaises(SourceOfflineError):
            verifier.verify("10.1111/uncached")

    def test_checks_expectations_matching(self) -> None:
        cr_payload = {
            "status": "ok",
            "message": {
                "title": ["Thermal Conductivity of AlN Ceramics"],
                "author": [{"family": "Kidalov", "given": "Sergei"}],
                "publisher": "Elsevier",
                "published-print": {"date-parts": [[2010, 5, 20]]},
                "abstract": "Measured 200 W/m·K at room temperature.",
            },
        }
        transport = FakeTransport([
            ("api.crossref.org/works/", FakeResponse(json.dumps(cr_payload))),
        ])
        verifier = self._make_verifier(transport)

        # Success case
        res = verifier.verify(
            "10.1016/j.aln.2010.01",
            expect_title="AlN Ceramics",
            expect_author="Kidalov",
            expect_year=2010,
            check_claim=["200 W/m·K"],
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "verified")
        self.assertTrue(res.checks.title_match)
        self.assertTrue(res.checks.author_match)
        self.assertTrue(res.checks.year_match)
        self.assertTrue(res.checks.claim_matches.get("200 W/m·K"))

        # Title mismatch
        res_tm = verifier.verify("10.1016/j.aln.2010.01", expect_title="Diamond Films")
        self.assertFalse(res_tm.ok)
        self.assertEqual(res_tm.status, "mismatch")

        # Author mismatch
        res_am = verifier.verify("10.1016/j.aln.2010.01", expect_author="Einstein")
        self.assertFalse(res_am.ok)
        self.assertEqual(res_am.status, "mismatch")

        # Year mismatch
        res_ym = verifier.verify("10.1016/j.aln.2010.01", expect_year=1999)
        self.assertFalse(res_ym.ok)
        self.assertEqual(res_ym.status, "mismatch")

        # Claim unverified
        res_cm = verifier.verify("10.1016/j.aln.2010.01", check_claim=["500 W/m·K"])
        self.assertFalse(res_cm.ok)
        self.assertEqual(res_cm.status, "unverified")


ARXIV_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title type="html">arXiv Query: search_query=id_list=2401.12345</title>
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <published>2024-01-15T18:00:00Z</published>
    <updated>2024-01-15T18:00:00Z</updated>
    <title> High Thermal Conductivity of   \n  AlN Sintered Ceramics </title>
    <summary> We report room temperature thermal conductivity of 220 W/mK in Y2O3 doped AlN. </summary>
    <author>
      <name>Alice Smith</name>
    </author>
    <author>
      <name>Bob Jones</name>
    </author>
  </entry>
</feed>
"""

ARXIV_WITH_DOI_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.99999v1</id>
    <published>2024-02-01T10:00:00Z</published>
    <title>Published AlN Paper</title>
    <summary>Sample abstract</summary>
    <author><name>Carol White</name></author>
    <arxiv:doi>10.1103/PhysRevMaterials.3.014601</arxiv:doi>
  </entry>
</feed>
"""

ARXIV_ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/api/errors#invalid_id</id>
    <title>Error</title>
    <summary>incorrect id format</summary>
  </entry>
</feed>
"""


class TestArxivVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_verifier(self, transport: Any, **kwargs: Any) -> SourceVerifier:
        params: dict[str, Any] = {
            "cache_dir": self.cache_dir,
            "transports": transport,
            "sleeper": lambda s: None,
            "sleep": 0.0,
            "no_rate_state": True,
        }
        params.update(kwargs)
        return SourceVerifier(**params)

    def test_arxiv_success_basic_and_cache(self) -> None:
        transport = FakeTransport([
            ("export.arxiv.org/api/query", FakeResponse(ARXIV_SAMPLE_XML)),
        ])
        verifier = self._make_verifier(transport)

        # 1st call
        res = verifier.verify(
            "arXiv:2401.12345",
            expect_title="AlN Sintered Ceramics",
            expect_author="Alice Smith",
            expect_year=2024,
            check_claim=["220 W/mK"],
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "verified")
        self.assertEqual(res.identifier, "arXiv:2401.12345")
        self.assertEqual(res.identifier_type, "arxiv")
        self.assertEqual(res.kind, "preprint")
        self.assertEqual(res.registration_agency, "arxiv")
        self.assertEqual(res.via, "arxiv")
        self.assertEqual(res.issued, "2024-01-15")
        self.assertEqual(res.metadata.year, 2024)
        self.assertEqual(res.metadata.publisher, "arXiv")
        self.assertEqual(res.metadata.authors, ["Alice Smith", "Bob Jones"])
        self.assertEqual(
            res.metadata.title, "High Thermal Conductivity of AlN Sintered Ceramics"
        )
        self.assertEqual(
            res.okf_source,
            f"preprint:arXiv:2401.12345 issued=2024-01-15 retrieved={res.retrieved_at} via=arxiv",
        )
        self.assertFalse(res.evidence.cached)
        self.assertEqual(len(transport.requests), 1)

        # 2nd call: cache hit
        res2 = verifier.verify("2401.12345")
        self.assertTrue(res2.ok)
        self.assertTrue(res2.evidence.cached)
        self.assertEqual(len(transport.requests), 1)

    def test_arxiv_published_doi_warning(self) -> None:
        transport = FakeTransport([
            ("export.arxiv.org/api/query", FakeResponse(ARXIV_WITH_DOI_XML)),
        ])
        verifier = self._make_verifier(transport)
        res = verifier.verify("arXiv:2401.99999")
        self.assertTrue(res.ok)
        self.assertEqual(res.metadata.published_doi, "10.1103/PhysRevMaterials.3.014601")
        self.assertTrue(
            any("10.1103/PhysRevMaterials.3.014601" in w for w in res.warnings)
        )

    def test_arxiv_not_found(self) -> None:
        transport = FakeTransport([
            ("export.arxiv.org/api/query", FakeResponse(ARXIV_ERROR_XML)),
        ])
        verifier = self._make_verifier(transport)
        res = verifier.verify("arXiv:0000.00000")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "not_found")
        xml_cache = list(self.cache_dir.glob("**/*.xml"))
        self.assertEqual(len(xml_cache), 0)


HTML_SAMPLE = """<!DOCTYPE html>
<html>
<head>
  <meta name="citation_title" content="AlN Technical Evaluation 2024">
  <meta name="citation_publisher" content="Example Institute of Ceramics">
  <meta name="citation_publication_date" content="2024-05-01">
  <title>Old Fallback Title</title>
  <style>body { font-size: 14px; }</style>
  <script>var x = "hidden script text";</script>
</head>
<body>
  <h1>AlN Technical Evaluation 2024</h1>
  <p>The sintered substrate demonstrates room temperature thermal conductivity of 230 W/mK.</p>
</body>
</html>
"""


class TestUrlVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_verifier(self, transport: Any, **kwargs: Any) -> SourceVerifier:
        params: dict[str, Any] = {
            "cache_dir": self.cache_dir,
            "transports": transport,
            "sleeper": lambda s: None,
            "sleep": 0.0,
            "no_rate_state": True,
        }
        params.update(kwargs)
        return SourceVerifier(**params)

    def test_url_html_snapshot_and_publisher_extraction(self) -> None:
        transport = FakeTransport([
            ("example.com/aln-report.html", FakeResponse(
                HTML_SAMPLE,
                headers={"Content-Type": "text/html; charset=utf-8"}
            )),
        ])
        verifier = self._make_verifier(transport)

        res = verifier.verify(
            "https://example.com/aln-report.html",
            expect_title="AlN Technical Evaluation",
            check_claim=["230 W/mK"],
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "verified")
        self.assertEqual(res.identifier, "https://example.com/aln-report.html")
        self.assertEqual(res.identifier_type, "url")
        self.assertEqual(res.kind, "web")
        self.assertEqual(res.via, "web")
        self.assertEqual(res.issued, "2024-05-01")
        self.assertEqual(res.metadata.year, 2024)
        self.assertEqual(res.metadata.publisher, "Example Institute of Ceramics")
        self.assertEqual(res.metadata.title, "AlN Technical Evaluation 2024")
        self.assertNotIn("hidden script text", res.metadata.abstract or "")

        # Evidence files
        self.assertIsNotNone(res.evidence.snapshot_path)
        self.assertIsNotNone(res.evidence.meta_path)
        self.assertTrue(Path(res.evidence.snapshot_path).exists())
        self.assertTrue(Path(res.evidence.meta_path).exists())

        # Check extracted text sidecar
        extracted_path = Path(res.evidence.snapshot_path).with_name(
            f"{Path(res.evidence.snapshot_path).stem}.extracted.txt"
        )
        self.assertTrue(extracted_path.exists())
        extracted_text = extracted_path.read_text("utf-8")
        self.assertIn("230 W/mK", extracted_text)
        self.assertNotIn("hidden script text", extracted_text)

        # Check .meta.json sidecar content
        meta_json = json.loads(Path(res.evidence.meta_path).read_text("utf-8"))
        self.assertEqual(meta_json["url"], "https://example.com/aln-report.html")
        self.assertEqual(meta_json["final_url"], "https://example.com/aln-report.html")
        self.assertEqual(meta_json["extractor"], "html.parser")
        self.assertEqual(
            meta_json["extracted_sha256"],
            f"sha256:{hashlib.sha256(extracted_text.encode('utf-8')).hexdigest()}",
        )
        self.assertEqual(meta_json["extracted_publisher"], "Example Institute of Ceramics")
        self.assertEqual(meta_json["extracted_issued"], "2024-05-01")
        self.assertEqual(meta_json["sha256"], res.evidence.sha256)

        # OKF source does NOT have sha256 or publisher
        self.assertNotIn("sha256", res.okf_source or "")
        self.assertNotIn("publisher", res.okf_source or "")
        self.assertEqual(
            res.okf_source,
            f"web:https://example.com/aln-report.html issued=2024-05-01 retrieved={res.retrieved_at} via=web",
        )

    def test_url_publisher_fallback_order(self) -> None:
        html_tr = """<html><head>
          <meta name="citation_technical_report_institution" content="Tech Lab Corp">
          <title>Tech Report</title>
        </head><body><p>Content</p></body></html>"""
        transport1 = FakeTransport([("example.com/tr", FakeResponse(html_tr))])
        v1 = self._make_verifier(transport1)
        res1 = v1.verify("https://example.com/tr")
        self.assertEqual(res1.metadata.publisher, "Tech Lab Corp")

        html_dc = """<html><head>
          <meta name="dc.publisher" content="DC Publisher Inc">
          <title>DC Page</title>
        </head><body><p>Content</p></body></html>"""
        transport2 = FakeTransport([("example.com/dc", FakeResponse(html_dc))])
        v2 = self._make_verifier(transport2)
        res2 = v2.verify("https://example.com/dc")
        self.assertEqual(res2.metadata.publisher, "DC Publisher Inc")

        html_og = """<html><head>
          <meta property="og:site_name" content="Site Name Org">
          <title>OG Page</title>
        </head><body><p>Content</p></body></html>"""
        transport3 = FakeTransport([("example.com/og", FakeResponse(html_og))])
        v3 = self._make_verifier(transport3)
        res3 = v3.verify("https://example.com/og")
        self.assertEqual(res3.metadata.publisher, "Site Name Org")

        html_none = """<html><head><title>No Publisher</title></head><body><p>None</p></body></html>"""
        transport4 = FakeTransport([("example.com/none", FakeResponse(html_none))])
        v4 = self._make_verifier(transport4)
        res4 = v4.verify("https://example.com/none")
        self.assertIsNone(res4.metadata.publisher)

    def test_url_issued_date_parsing_and_cli_override(self) -> None:
        html_year_only = """<html><head>
          <meta name="citation_publication_date" content="2023">
          <title>Year Only</title>
        </head><body><p>Content</p></body></html>"""
        transport = FakeTransport([
            ("example.com/year", FakeResponse(html_year_only)),
            ("example.com/override", FakeResponse(html_year_only)),
        ])
        v = self._make_verifier(transport)

        # Year only: issued is None
        res1 = v.verify("https://example.com/year")
        self.assertIsNone(res1.issued)
        self.assertEqual(res1.metadata.year, 2023)
        self.assertNotIn("issued=", res1.okf_source or "")

        # Explicit --issued override
        res2 = v.verify("https://example.com/override", issued="2023-11-01")
        self.assertEqual(res2.issued, "2023-11-01")
        self.assertIn("issued=2023-11-01", res2.okf_source or "")
        meta_json = json.loads(Path(res2.evidence.meta_path).read_text("utf-8"))
        self.assertIsNone(meta_json["extracted_issued"])  # extracted remains null

    def test_url_snapshot_cache_claim_recheck(self) -> None:
        html = "<html><body><p>High thermal conductivity 170 W/mK.</p></body></html>"
        transport = FakeTransport([("example.com/test", FakeResponse(html))])
        v = self._make_verifier(transport)

        # 1st call
        res1 = v.verify("https://example.com/test", check_claim=["170 W/mK"])
        self.assertTrue(res1.ok)

        # 2nd call offline with different claim
        v_off = self._make_verifier(FakeTransport([]), offline=True)
        res2 = v_off.verify("https://example.com/test", check_claim=["170 W/mK"])
        self.assertTrue(res2.ok)
        self.assertTrue(res2.evidence.cached)

        # 2nd call offline with missing claim
        res3 = v_off.verify("https://example.com/test", check_claim=["999 W/mK"])
        self.assertFalse(res3.ok)
        self.assertEqual(res3.status, "unverified")

    def test_url_http_403_paywall_no_retry(self) -> None:
        import urllib.error

        def raise_403():
            raise urllib.error.HTTPError(
                "https://example.com/paywalled", 403, "Forbidden", {}, None
            )

        transport = FakeTransport([("example.com/paywalled", raise_403)])
        v = self._make_verifier(transport)
        res = v.verify("https://example.com/paywalled")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "unverified")
        self.assertEqual(len(transport.requests), 1)  # No retry

    def test_url_pdf_support_and_fallback(self) -> None:
        # Minimal valid PDF binary
        pdf_bytes = (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 300 144]/Parent 2 0 R/Resources<<>>>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f\n0000000010 00000 n\n0000000053 00000 n\n0000000102 00000 n\n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF\n"
        )
        transport = FakeTransport([
            ("example.com/doc.pdf", FakeResponse(
                pdf_bytes,
                headers={"Content-Type": "application/pdf"}
            )),
        ])
        v = self._make_verifier(transport)
        res = v.verify("https://example.com/doc.pdf")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "verified")
        self.assertTrue(res.evidence.snapshot_path.endswith(".pdf"))
        self.assertTrue(Path(res.evidence.snapshot_path).exists())

    def test_url_max_snapshot_bytes_exceeded(self) -> None:
        big_body = b"X" * (10 * 1024 * 1024 + 10)
        transport = FakeTransport([("example.com/big", FakeResponse(big_body))])
        v = self._make_verifier(transport)
        with self.assertRaises(SourceVerifyError):
            v.verify("https://example.com/big")

    def test_url_pdf_fallback_without_pypdf(self) -> None:
        import sys
        from unittest.mock import patch

        pdf_bytes = b"%PDF-1.4 sample non-compressed text 150 W/mK %%EOF"
        transport = FakeTransport([
            ("example.com/manual.pdf", FakeResponse(
                pdf_bytes,
                headers={"Content-Type": "application/pdf"}
            )),
        ])
        v = self._make_verifier(transport)

        # Simulate environment where pypdf is not installed
        with patch.dict(sys.modules, {"pypdf": None}):
            # Without claim check, PDF verification succeeds
            res1 = v.verify("https://example.com/manual.pdf")
            self.assertTrue(res1.ok)

            # With claim check that cannot be extracted, status is unverified with warning
            v_recheck = self._make_verifier(transport, refresh=True)
            res2 = v_recheck.verify("https://example.com/manual.pdf", check_claim=["nonexistent_val"])
            self.assertFalse(res2.ok)
            self.assertEqual(res2.status, "unverified")
            self.assertTrue(any("pypdf" in w for w in res2.warnings))

    def test_url_redirect_records_true_final_url(self) -> None:
        transport = FakeTransport([
            ("example.com/files/4731/download", FakeResponse(
                HTML_SAMPLE,
                headers={"Content-Type": "text/html; charset=utf-8"},
                url="https://cdn.example.com/docs/hexoloy-datasheet.html",
            )),
        ])
        verifier = self._make_verifier(transport)
        res = verifier.verify("https://example.com/files/4731/download")
        self.assertTrue(res.ok)
        meta_json = json.loads(Path(res.evidence.meta_path).read_text("utf-8"))
        self.assertEqual(meta_json["url"], "https://example.com/files/4731/download")
        self.assertEqual(meta_json["final_url"], "https://cdn.example.com/docs/hexoloy-datasheet.html")

    def test_url_pdf_extracted_sidecar_with_and_without_pypdf(self) -> None:
        import sys
        from unittest.mock import MagicMock, patch

        pdf_bytes = b"%PDF-1.4 dummy binary content"
        transport1 = FakeTransport([
            ("example.com/pypdf.pdf", FakeResponse(
                pdf_bytes,
                headers={"Content-Type": "application/pdf"}
            )),
        ])
        v1 = self._make_verifier(transport1)
        mock_pypdf = MagicMock()
        mock_reader = MagicMock()
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Sample PDF extracted text 150 W/mK"
        mock_reader.pages = [mock_page]
        mock_pypdf.PdfReader.return_value = mock_reader

        with patch.dict(sys.modules, {"pypdf": mock_pypdf}):
            res1 = v1.verify("https://example.com/pypdf.pdf")
            self.assertTrue(res1.ok)
            meta1 = json.loads(Path(res1.evidence.meta_path).read_text("utf-8"))
            self.assertEqual(meta1["extractor"], "pypdf")
            extracted_path1 = Path(res1.evidence.snapshot_path).with_name(
                f"{Path(res1.evidence.snapshot_path).stem}.extracted.txt"
            )
            self.assertTrue(extracted_path1.exists())
            txt1 = extracted_path1.read_text("utf-8")
            self.assertEqual(txt1, "Sample PDF extracted text 150 W/mK")
            self.assertEqual(
                meta1["extracted_sha256"],
                f"sha256:{hashlib.sha256(txt1.encode('utf-8')).hexdigest()}",
            )

        # Without pypdf
        pdf_ascii_bytes = b"%PDF-1.4 sample non-compressed text 150 W/mK %%EOF"
        transport2 = FakeTransport([
            ("example.com/fallback.pdf", FakeResponse(
                pdf_ascii_bytes,
                headers={"Content-Type": "application/pdf"}
            )),
        ])
        v2 = self._make_verifier(transport2)
        with patch.dict(sys.modules, {"pypdf": None}):
            res2 = v2.verify("https://example.com/fallback.pdf")
            self.assertTrue(res2.ok)
            meta2 = json.loads(Path(res2.evidence.meta_path).read_text("utf-8"))
            self.assertEqual(meta2["extractor"], "ascii_fallback")
            extracted_path2 = Path(res2.evidence.snapshot_path).with_name(
                f"{Path(res2.evidence.snapshot_path).stem}.extracted.txt"
            )
            self.assertTrue(extracted_path2.exists())
            txt2 = extracted_path2.read_text("utf-8")
            self.assertIn("150 W/mK", txt2)
            self.assertEqual(
                meta2["extracted_sha256"],
                f"sha256:{hashlib.sha256(txt2.encode('utf-8')).hexdigest()}",
            )

    def test_url_cache_hit_uses_saved_extracted_txt_without_reparse(self) -> None:
        import sys
        from unittest.mock import MagicMock, patch

        pdf_bytes = b"%PDF-1.4 dummy binary content"
        transport = FakeTransport([
            ("example.com/doc.pdf", FakeResponse(
                pdf_bytes,
                headers={"Content-Type": "application/pdf"}
            )),
        ])
        mock_pypdf = MagicMock()
        mock_reader = MagicMock()
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Initial extracted text"
        mock_reader.pages = [mock_page]
        mock_pypdf.PdfReader.return_value = mock_reader

        v1 = self._make_verifier(transport)
        with patch.dict(sys.modules, {"pypdf": mock_pypdf}):
            res1 = v1.verify("https://example.com/doc.pdf")
            self.assertTrue(res1.ok)
            extracted_path = Path(res1.evidence.snapshot_path).with_name(
                f"{Path(res1.evidence.snapshot_path).stem}.extracted.txt"
            )
            self.assertTrue(extracted_path.exists())

        # Modify .extracted.txt to include new claim
        extracted_path.write_text("cached extracted text with 999 W/mK", encoding="utf-8")

        # 2nd call: offline, pypdf is None (not installed), check_claim=["999 W/mK"]
        v2 = self._make_verifier(FakeTransport([]), offline=True)
        with patch.dict(sys.modules, {"pypdf": None}):
            res2 = v2.verify("https://example.com/doc.pdf", check_claim=["999 W/mK"])
            self.assertTrue(res2.ok)
            self.assertEqual(res2.status, "verified")
            self.assertTrue(res2.evidence.cached)
            self.assertTrue(res2.evidence.snapshot_path.endswith(".pdf"))
            self.assertFalse(res2.evidence.snapshot_path.endswith(".extracted.txt"))
            self.assertIn("999 W/mK", res2.metadata.abstract or "")

    def test_url_legacy_snapshot_without_extracted_txt_fallback(self) -> None:
        transport = FakeTransport([
            ("example.com/legacy-doc.html", FakeResponse(
                HTML_SAMPLE,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )),
        ])
        v_init = self._make_verifier(transport)
        res_init = v_init.verify("https://example.com/legacy-doc.html")
        self.assertTrue(res_init.ok)

        # Revert to legacy snapshot: delete .extracted.txt and strip new fields from .meta.json
        meta_path = Path(res_init.evidence.meta_path)
        extracted_path = Path(res_init.evidence.snapshot_path).with_name(
            f"{Path(res_init.evidence.snapshot_path).stem}.extracted.txt"
        )
        self.assertTrue(extracted_path.exists())
        extracted_path.unlink()
        self.assertFalse(extracted_path.exists())

        meta_json = json.loads(meta_path.read_text("utf-8"))
        meta_json.pop("extractor", None)
        meta_json.pop("extracted_sha256", None)
        meta_path.write_text(json.dumps(meta_json, indent=2), encoding="utf-8")

        # Now test offline cache hit on legacy snapshot
        v = self._make_verifier(FakeTransport([]), offline=True)
        res = v.verify("https://example.com/legacy-doc.html", check_claim=["230 W/mK"])
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "verified")
        self.assertTrue(res.evidence.cached)
        self.assertEqual(res.metadata.title, "AlN Technical Evaluation 2024")
        self.assertIn("230 W/mK", res.metadata.abstract or "")

        # Strictly Read-Only: .extracted.txt should NOT be created lazily
        self.assertFalse(extracted_file := extracted_path.exists())


if __name__ == "__main__":
    unittest.main()
