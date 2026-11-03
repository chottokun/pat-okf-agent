"""Tests for EPO -> OKF ingestion (offline fake client)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.epo import EpoApiError, PublicationRef, SearchResult
from pat_okf_agent.wiki.frontmatter import parse_frontmatter
from pat_okf_agent.wiki.ingest import ingest_publication, ingest_query

NOW = "2026-10-29T00:00:00Z"


def biblio(number: str = "9999999", title: str = "Amorphous alloy") -> dict:
    return {
        "title": title,
        "applicants": ["EXAMPLE CORP"],
        "inventors": ["TARO EXAMPLE"],
        "priority": "JP2014000001 20140101",
        "family_id": "99999999",
        "country": "EP",
        "number": number,
        "kind": "B1",
        "publication_date": "20180131",
    }


class FakeEpo:
    def __init__(self, *, biblio_data=None, family=None, claims=None,
                 claims_error=None, search_result=None, by_publication=None):
        self.biblio_data = biblio_data or biblio()
        self.family_members = family if family is not None else []
        self.claim_list = claims if claims is not None else [
            "1. A composition comprising an amorphous phase.",
            "2. The composition of claim 1, further comprising a metal.",
        ]
        self.claims_error = claims_error
        self.search_result = search_result
        self.by_publication = by_publication or {}
        self.calls = []

    def biblio(self, publication, **_kwargs):
        self.calls.append(("biblio", publication))
        return dict(self.by_publication.get(publication, self.biblio_data))

    def family(self, publication, **_kwargs):
        self.calls.append(("family", publication))
        return list(self.family_members)

    def claims(self, publication, **_kwargs):
        self.calls.append(("claims", publication))
        if self.claims_error:
            raise self.claims_error
        return list(self.claim_list)

    def search(self, cql, *, begin=1, end=25, **_kwargs):
        self.calls.append(("search", cql, begin, end))
        return self.search_result or SearchResult(cql, 0, [])


class FakeEps:
    def __init__(self, claims=None, error=None):
        self.claim_list = claims or ["1. EPS claim."]
        self.error = error
        self.calls = 0

    def claims(self, publication, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.claim_list)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def read(self, rel: str) -> tuple[dict, str]:
        return parse_frontmatter((self.root / rel).read_text(encoding="utf-8"))

    def test_publication_becomes_an_okf_node(self):
        family = [
            PublicationRef("EP", "9999999", "B1", "20180131", "99999999"),
            PublicationRef("US", "1234567", "B2", "20190205", "99999999"),
        ]
        client = FakeEpo(family=family)
        summary = ingest_publication(client, "EP9999999B1", self.root,
                                     now=NOW)
        rel = "patents/EP9999999B1.md"
        self.assertTrue((self.root / rel).exists())
        meta, body = self.read(rel)
        self.assertEqual(meta["type"], "Patent")
        self.assertEqual(meta["title"], "Amorphous alloy")
        self.assertEqual(meta["timestamp"], NOW)
        self.assertEqual(meta["scope"], "taxonomy")
        self.assertIn("epo", meta["tags"])
        self.assertIn("ep", meta["tags"])
        self.assertIn("## Bibliographic", body)
        self.assertIn("EXAMPLE CORP", body)
        self.assertIn("## Claims", body)
        self.assertIn("A composition comprising an amorphous phase.", body)
        self.assertIn("## Family", body)
        self.assertIn("[US1234567B2](US1234567B2.md)", body)
        self.assertIn("patents/EP9999999B1.md", summary["node"])

    def test_family_members_become_stubs_with_backlinks(self):
        family = [PublicationRef("US", "1234567", "B2", "20190205", "1")]
        summary = ingest_publication(FakeEpo(family=family), "EP9999999B1",
                                     self.root, now=NOW)
        stub_rel = "patents/US1234567B2.md"
        self.assertTrue((self.root / stub_rel).exists())
        meta, body = self.read(stub_rel)
        self.assertEqual(meta["type"], "Patent")
        self.assertIn("stub", meta["tags"])
        self.assertIn("Family member", meta["description"])
        self.assertIn("[EP9999999B1](EP9999999B1.md)", body)
        self.assertIn(stub_rel, summary["stubs"])

    def test_ingesting_a_stub_later_promotes_it(self):
        family = [PublicationRef("US", "1234567", "B2", "20190205", "1")]
        client = FakeEpo(family=family)
        ingest_publication(client, "EP9999999B1", self.root, now=NOW)
        # The stub now gets a full record.
        client.by_publication["US1234567B2"] = biblio("1234567", "US alloy")
        client.family_members = []
        ingest_publication(client, "US1234567B2", self.root, now=NOW)
        meta, body = self.read("patents/US1234567B2.md")
        self.assertNotIn("stub", meta["tags"])
        self.assertEqual(meta["title"], "US alloy")
        self.assertIn("## Claims", body)
        # Re-ingesting the parent must not downgrade the full node to a stub.
        client.family_members = family
        ingest_publication(client, "EP9999999B1", self.root, now=NOW)
        meta, _ = self.read("patents/US1234567B2.md")
        self.assertNotIn("stub", meta["tags"])

    def test_claims_failure_keeps_the_node_and_reports_a_warning(self):
        client = FakeEpo(claims_error=EpoApiError("not available", status=404))
        summary = ingest_publication(client, "EP9999999B1", self.root,
                                     now=NOW)
        meta, body = self.read("patents/EP9999999B1.md")
        self.assertEqual(meta["type"], "Patent")
        self.assertIn("Claims not available", body)
        self.assertTrue(summary["warnings"])

    def test_eps_claims_fallback_when_ops_claims_fail(self):
        client = FakeEpo(claims_error=EpoApiError("not available", status=404))
        eps = FakeEps(claims=["1. An EPS claim."])

        summary = ingest_publication(client, "EP9999999B1", self.root,
                                     eps_client=eps, now=NOW)
        _, body = self.read("patents/EP9999999B1.md")
        self.assertIn("An EPS claim.", body)
        self.assertEqual(summary["claims_source"], "EPO EPS")
        self.assertEqual(summary["claims"], 1)
        self.assertFalse(summary["warnings"])
        self.assertEqual(eps.calls, 1)

    def test_eps_is_not_called_when_ops_claims_succeed(self):
        eps = FakeEps()
        ingest_publication(FakeEpo(), "EP9999999B1", self.root,
                           eps_client=eps, now=NOW)
        self.assertEqual(eps.calls, 0)

    def test_eps_fallback_is_ep_only(self):
        client = FakeEpo(claims_error=EpoApiError("not available", status=404))
        eps = FakeEps()
        ingest_publication(client, "US1234567B2", self.root,
                           eps_client=eps, now=NOW)
        self.assertEqual(eps.calls, 0)

    def test_claims_and_family_can_be_disabled(self):
        client = FakeEpo(family=[PublicationRef("US", "1", "A1")])
        ingest_publication(client, "EP9999999B1", self.root, now=NOW,
                           include_claims=False, include_family=False)
        _, body = self.read("patents/EP9999999B1.md")
        self.assertNotIn("## Claims", body)
        self.assertFalse((self.root / "patents/US1A1.md").exists())

    def test_family_without_stubs_is_listed_as_text_not_a_broken_link(self):
        client = FakeEpo(family=[PublicationRef("US", "1", "A1")])
        ingest_publication(client, "EP9999999B1", self.root, now=NOW,
                           create_stubs=False)
        _, body = self.read("patents/EP9999999B1.md")
        self.assertIn("- US1A1", body)
        self.assertNotIn("[US1A1](US1A1.md)", body)
        self.assertFalse((self.root / "patents/US1A1.md").exists())

    def test_ingestion_updates_index_and_log(self):
        ingest_publication(FakeEpo(), "EP9999999B1", self.root, now=NOW)
        index = (self.root / "patents/index.md").read_text(encoding="utf-8")
        self.assertIn("[Amorphous alloy](EP9999999B1.md)", index)
        log = (self.root / "log.md").read_text(encoding="utf-8")
        self.assertIn("EP9999999B1", log)
        self.assertIn(NOW, log)

    def test_reingestion_is_idempotent_for_the_node(self):
        client = FakeEpo()
        ingest_publication(client, "EP9999999B1", self.root, now=NOW)
        first = (self.root / "patents/EP9999999B1.md").read_text("utf-8")
        ingest_publication(client, "EP9999999B1", self.root, now=NOW)
        second = (self.root / "patents/EP9999999B1.md").read_text("utf-8")
        self.assertEqual(first, second)

    def test_query_ingests_only_the_search_window(self):
        pubs = [PublicationRef("EP", "1", "A1"), PublicationRef("EP", "2", "A1")]
        client = FakeEpo(search_result=SearchResult("ti=alloy", 99, pubs))
        client.by_publication["EP1A1"] = biblio("1", "One")
        client.by_publication["EP2A1"] = biblio("2", "Two")
        summary = ingest_query(client, "ti=alloy", self.root, max_results=2,
                               now=NOW)
        self.assertEqual(len(summary["publications"]), 2)
        self.assertTrue((self.root / "patents/EP1A1.md").exists())
        self.assertTrue((self.root / "patents/EP2A1.md").exists())
        search_call = [c for c in client.calls if c[0] == "search"][0]
        self.assertEqual(search_call[2:], (1, 2))

    def test_query_results_above_the_free_window_are_capped(self):
        client = FakeEpo(search_result=SearchResult("q", 0, []))
        ingest_query(client, "q", self.root, max_results=999, now=NOW)
        search_call = [c for c in client.calls if c[0] == "search"][0]
        self.assertEqual(search_call[3], 25)

    def test_output_directory_is_created(self):
        target = self.root / "deep" / "wiki"
        ingest_publication(FakeEpo(), "EP9999999B1", target, now=NOW)
        self.assertTrue((target / "patents/EP9999999B1.md").exists())
        self.assertTrue((target / "index.md").exists())


if __name__ == "__main__":
    unittest.main()
