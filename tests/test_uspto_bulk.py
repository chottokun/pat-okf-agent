"""Offline tests for the USPTO bulk download / grant XML parsing module.

TDD contract for ``okf_kb_agent.uspto_bulk``:
- ODP public/authenticated file URL construction
- streaming download with length verification and 503 retry
- ZIP extraction with path-traversal protection
- parsing of USPTO grant XML (single and multi-document)
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path

from okf_kb_agent.uspto_bulk import (
    BulkError,
    download,
    download_year,
    extract_zip,
    iter_grant_documents,
    iter_grant_documents_zip,
    parse_grant_xml,
    product_file_url,
    product_files,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = FIXTURES / "us_grant_sample.xml"


class FakeStream:
    def __init__(self, body: bytes, headers=None, chunks=4):
        self._buf = io.BytesIO(body)
        self._size = max(1, len(body) // max(1, chunks))
        self.headers = headers or {}
        self.status = 200

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._buf.getvalue())
        return self._buf.read(min(n, self._size))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeBulkClient:
    """Mimics UsptoClient.get_json for product listing, keyed by offset."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_json(self, path, *, params=None, name=None, refresh=False):
        self.calls.append((path, dict(params or {}), name))
        return self.pages[params.get("offset", 0)]


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_download_writes_file_and_verifies_length(self):
        payload = b"<us-patent-grant>" + b"x" * 500 + b"</us-patent-grant>"

        def opener(request, timeout):
            return FakeStream(payload, {"Content-Length": str(len(payload))})

        target = self.dir / "doc.xml"
        size = download("https://example.test/doc.xml", target, opener=opener,
                        sleep_s=0)
        self.assertEqual(size, len(payload))
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse((self.dir / "doc.xml.part").exists())

    def test_download_detects_truncation(self):
        payload = b"short"

        def opener(request, timeout):
            return FakeStream(payload, {"Content-Length": "999"})

        with self.assertRaises(BulkError):
            download("https://example.test/doc.xml", self.dir / "doc.xml",
                     opener=opener, sleep_s=0)
        self.assertFalse((self.dir / "doc.xml").exists())
        self.assertFalse((self.dir / "doc.xml.part").exists())

    def test_download_retries_503(self):
        attempts = {"n": 0}
        payload = b"ok"

        def opener(request, timeout):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.HTTPError(
                    "https://example.test/x", 503, "busy", {},
                    io.BytesIO(b"busy"))
            return FakeStream(payload, {"Content-Length": str(len(payload))})

        sleeps = []
        size = download("https://example.test/x", self.dir / "x.bin",
                        opener=opener, sleep_s=0, sleeper=sleeps.append)
        self.assertEqual(size, 2)
        self.assertEqual(attempts["n"], 2)
        self.assertTrue(sleeps)

    def test_api_key_header_when_requested(self):
        seen = {}

        def opener(request, timeout):
            seen["key"] = request.get_header("X-api-key")
            body = b"ok"
            return FakeStream(body, {"Content-Length": "2"})

        download("https://api.uspto.gov/api/v1/datasets/products/files/P/2024/f.zip",
                 self.dir / "f.zip", opener=opener, api_key="secret", sleep_s=0)
        self.assertEqual(seen["key"], "secret")


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _zip(self, name, members):
        path = self.dir / name
        with zipfile.ZipFile(path, "w") as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return path

    def test_extract_zip(self):
        archive = self._zip("ok.zip", {"ipg240604/patent.xml": "<x/>",
                                       "readme.txt": "hi"})
        out = self.dir / "out"
        files = extract_zip(archive, out)
        self.assertEqual(len(files), 2)
        self.assertTrue((out / "ipg240604" / "patent.xml").exists())

    def test_extract_zip_rejects_path_traversal(self):
        archive = self._zip("evil.zip", {"../evil.txt": "boom"})
        with self.assertRaises(BulkError):
            extract_zip(archive, self.dir / "out")
        self.assertFalse((self.dir / "evil.txt").exists())


class GrantXmlTests(unittest.TestCase):
    def test_parse_single_grant(self):
        doc = parse_grant_xml(SAMPLE)
        self.assertEqual(doc["publication_number"], "US12345678B2")
        self.assertEqual(doc["kind"], "B2")
        self.assertIn("SAMPLE COMPOSITION", doc["title"])
        self.assertIn("amorphous alloy", doc["abstract"])
        self.assertEqual(doc["inventors"], ["Jane Doe", "John Smith"])
        self.assertEqual(doc["assignees"], ["ACME CORPORATION"])
        self.assertEqual(len(doc["claims"]), 2)
        self.assertTrue(doc["claims"][0].startswith("1. A composition"))

    def test_iter_multiple_documents(self):
        wrapper = ("<patent-documents>"
                   + SAMPLE.read_text(encoding="utf-8").split("?>", 1)[1]
                   + SAMPLE.read_text(encoding="utf-8").split("?>", 1)[1]
                   + "</patent-documents>")
        # strip DOCTYPEs inside the wrapper for well-formedness
        wrapper = wrapper.replace(
            '<!DOCTYPE us-patent-grant SYSTEM "us-patent-grant-v45-2014-04-03.dtd" [\n]>',
            "")
        path = Path(tempfile.mkdtemp()) / "multi.xml"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_text(wrapper, encoding="utf-8")
        docs = list(iter_grant_documents(path))
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0]["publication_number"], "US12345678B2")

    def test_non_grant_xml_is_rejected(self):
        bad = Path(tempfile.mkdtemp()) / "bad.xml"
        self.addCleanup(lambda: bad.unlink(missing_ok=True))
        bad.write_text("<html><body/></html>", encoding="utf-8")
        with self.assertRaises(BulkError):
            parse_grant_xml(bad)

    def test_iter_zip_documents(self):
        import zipfile
        zpath = Path(tempfile.mkdtemp()) / "week.zip"
        self.addCleanup(lambda: zpath.unlink(missing_ok=True))
        payload = SAMPLE.read_bytes()
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("ipg240102.xml", payload)
            zf.writestr("nested/ipg240109.xml", payload)
        docs = list(iter_grant_documents_zip(zpath))
        self.assertEqual(len(docs), 2)
        self.assertEqual({d["publication_number"] for d in docs},
                         {"US12345678B2"})
        self.assertEqual(docs[0]["claim_count"], 2)

    def test_iter_zip_rejects_unsafe_member(self):
        import zipfile
        zpath = Path(tempfile.mkdtemp()) / "evil.zip"
        self.addCleanup(lambda: zpath.unlink(missing_ok=True))
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("../evil.xml", SAMPLE.read_bytes())
        with self.assertRaises(BulkError):
            list(iter_grant_documents_zip(zpath))


class YearDownloadTests(unittest.TestCase):
    """Year-level aggregation: product file listing + download orchestration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    @staticmethod
    def _payload(files, total=None):
        total = len(files) if total is None else total
        return {"count": 1, "bulkDataProductBag": [{
            "productIdentifier": "PTGRXML",
            "productFileBag": {"count": total, "fileDataBag": files},
        }]}

    def test_product_files_requests_year_range_and_paginates(self):
        pages = {
            0: self._payload([
                {"fileName": "ipg240102.zip", "fileSize": 10,
                 "fileDataFromDate": "2024-01-01", "fileDataToDate": "2024-01-07"},
                {"fileName": "ipg240109.zip", "fileSize": 11,
                 "fileDataFromDate": "2024-01-08", "fileDataToDate": "2024-01-14"},
            ], total=3),
            2: self._payload([
                {"fileName": "ipg240116.zip", "fileSize": 12,
                 "fileDataFromDate": "2024-01-15", "fileDataToDate": "2024-01-21"},
            ], total=3),
        }
        client = FakeBulkClient(pages)
        files = product_files(client, "PTGRXML", year=2024, limit=2)
        self.assertEqual([f["fileName"] for f in files],
                         ["ipg240102.zip", "ipg240109.zip", "ipg240116.zip"])
        first = client.calls[0][1]
        self.assertEqual(first["fileDataFromDate"], "2024-01-01")
        self.assertEqual(first["fileDataToDate"], "2024-12-31")
        self.assertEqual(first["includeFiles"], "true")
        self.assertEqual(client.calls[1][1]["offset"], 2)

    def test_download_year_skips_identical_and_downloads_rest(self):
        payload = self._payload([
            {"fileName": "ipg240102.zip", "fileSize": 3,
             "fileDataFromDate": "2024-01-01", "fileDataToDate": "2024-01-07"},
            {"fileName": "sub/ipg240109.zip", "fileSize": 4,
             "fileDataFromDate": "2024-01-08", "fileDataToDate": "2024-01-14"},
        ])
        client = FakeBulkClient({0: payload})
        out = self.dir / "2024"
        (out).mkdir()
        (out / "ipg240102.zip").write_bytes(b"abc")  # same size -> skip
        body = b"data"

        def opener(request, timeout):
            return FakeStream(body, {"Content-Length": str(len(body))})

        summary = download_year(client, "PTGRXML", 2024, out,
                                opener=opener, sleep_s=0)
        statuses = {row["file"]: row["status"] for row in summary["files"]}
        self.assertEqual(statuses["ipg240102.zip"], "skipped")
        self.assertEqual(statuses["sub/ipg240109.zip"], "ok")
        self.assertEqual(summary["total_bytes"], 4)
        self.assertTrue((out / "sub" / "ipg240109.zip").exists())

    def test_download_year_dry_run(self):
        payload = self._payload([
            {"fileName": "ipg240102.zip", "fileSize": 3,
             "fileDataFromDate": "2024-01-01", "fileDataToDate": "2024-01-07"},
        ])
        summary = download_year(FakeBulkClient({0: payload}), "PTGRXML", 2024,
                                self.dir / "2024", dry_run=True)
        self.assertEqual(summary["files"][0]["status"], "planned")
        self.assertFalse((self.dir / "2024" / "ipg240102.zip").exists())

    def test_unsafe_file_name_is_rejected(self):
        payload = self._payload([
            {"fileName": "../evil.zip", "fileSize": 1,
             "fileDataFromDate": "2024-01-01", "fileDataToDate": "2024-01-07"},
        ])
        with self.assertRaises(BulkError):
            download_year(FakeBulkClient({0: payload}), "PTGRXML", 2024,
                          self.dir / "2024", dry_run=True)


class UrlTests(unittest.TestCase):
    def test_product_file_url(self):
        url = product_file_url("PTGRXML-SPLT", 2024, "ipg240604",
                               "18999999_12000000.xml")
        self.assertEqual(
            url,
            "https://api.uspto.gov/api/v1/datasets/products/files/"
            "PTGRXML-SPLT/2024/ipg240604/18999999_12000000.xml")


if __name__ == "__main__":
    unittest.main()
